package com.echopersona.echo

import android.annotation.SuppressLint
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.sqrt

/** The microphone: 16 kHz mono, stops by itself after ~1.2 s of quiet once someone has spoken (or 30 s at most). The mic
 *  is open ONLY between start() and the end of that one utterance. */
class Recorder {
    val level = MutableStateFlow(0f)       // 0..1, drives the ring while listening
    @Volatile private var running = false

    @SuppressLint("MissingPermission")
    fun record(quietLimitMs: Int = 1800, noSpeechMs: Int = 30000, onDone: (ByteArray?) -> Unit) {
        val rate = 16000
        val min = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = try {
            AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, rate, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, maxOf(min, 3200) * 2)
        } catch (e: Exception) { onDone(null); return }
        if (rec.state != AudioRecord.STATE_INITIALIZED) { rec.release(); onDone(null); return }
        running = true
        Thread {
            val pcm = ByteArrayOutputStream(); val buf = ShortArray(1600)    // 0.1 s
            var noise = 0.01f; var spoke = false; var quietMs = 0; var totalMs = 0
            rec.startRecording()
            while (running && totalMs < 30000) {
                val n = rec.read(buf, 0, buf.size); if (n <= 0) break
                var sum = 0.0; for (i in 0 until n) { val v = buf[i] / 32768.0; sum += v * v }
                val rms = sqrt(sum / n).toFloat()
                level.value = (rms * 8f).coerceIn(0f, 1f)
                if (!spoke) noise = noise * 0.9f + rms * 0.1f
                if (rms > maxOf(0.02f, noise * 3f)) { spoke = true; quietMs = 0 } else if (spoke) quietMs += 100
                val bb = ByteBuffer.allocate(n * 2).order(ByteOrder.LITTLE_ENDIAN); for (i in 0 until n) bb.putShort(buf[i])
                pcm.write(bb.array()); totalMs += 100
                if (spoke && quietMs >= quietLimitMs) break
                if (!spoke && totalMs >= noSpeechMs) break        // follow-up listening: nothing more was said
            }
            rec.stop(); rec.release(); level.value = 0f
            val wasStopped = !running; running = false
            onDone(if (spoke || wasStopped) wav(pcm.toByteArray(), rate) else ByteArray(0))
        }.start()
    }

    /** Tap again: stop now and send what was said. */
    fun finish() { running = false }

    private fun wav(pcm: ByteArray, rate: Int): ByteArray {
        val h = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN)
        h.put("RIFF".toByteArray()).putInt(36 + pcm.size).put("WAVE".toByteArray()).put("fmt ".toByteArray())
            .putInt(16).putShort(1).putShort(1).putInt(rate).putInt(rate * 2).putShort(2).putShort(16)
            .put("data".toByteArray()).putInt(pcm.size)
        return h.array() + pcm
    }
}

/** Echo's voice. Sentences are spoken in order as the reply streams in, all through ONE speaker for the whole reply
 *  (no clunk between sentences). Each sentence's audio starts downloading as soon as the sentence is known.
 *  - It waits for a small reserve before speaking, and if the Mac falls behind (it can be slower than real time while
 *    it's still writing the reply) it pauses cleanly and refills, instead of stuttering mid-word.
 *  - It always lets the speaker play out to the last sample before closing it (no clipped endings).
 *  - One long-lived loop takes sentences from the queue, so a sentence that arrives at the last moment is never lost.
 *  stop() = interrupted (tap the ring while he talks): silence now, and the Mac stops making the rest. */
class Speaker(private val link: () -> EchoLink?) {
    val level = MutableStateFlow(0f)
    val speaking = MutableStateFlow(false)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var queue = Channel<String>(Channel.UNLIMITED)
    private var job: Job? = null
    private val clips = java.util.concurrent.ConcurrentLinkedQueue<Clip>()
    @Volatile private var out: Out? = null
    @Volatile private var gen = 0

    private companion object {
        const val START_MS = 450      // reserve before a sentence starts (the Mac sends ~0.8 s pieces)
        const val REFILL_MS = 1300    // reserve after running dry in the middle of a sentence
        const val MAX_FILL_MS = 4000  // never wait longer than this (the track holds 6 s)
        const val LOW_MS = 80         // "about to run dry"
        const val GRACE_MS = 600L     // after the last sound, wait this long for another sentence before closing
    }

    @Synchronized fun say(sentence: String) {
        if (sentence.isBlank()) return
        if (job?.isActive != true) { val q = queue; val g = gen; job = scope.launch { loop(q, g) } }
        queue.trySend(sentence)
    }

    @Synchronized fun stop() {
        gen++; job?.cancel(); job = null
        queue.close(); queue = Channel(Channel.UNLIMITED)
        while (true) clips.poll()?.cancel() ?: break
        out?.kill(); out = null
        level.value = 0f; speaking.value = false
    }

    /** One sentence's audio, downloading in the background into [chunks]; an empty array marks the end. */
    private inner class Clip(val text: String) {
        val chunks = java.util.concurrent.LinkedBlockingQueue<ByteArray>()
        @Volatile var rate = 24000
        private val call = link()?.ttsCall(text)
        init {
            clips.add(this)
            Thread {
                try {
                    call?.execute()?.use { r ->
                        if (r.isSuccessful) {
                            rate = r.header("X-Sample-Rate")?.toIntOrNull() ?: 24000
                            val src = r.body!!.byteStream(); val buf = ByteArray(8192)
                            while (true) { val n = src.read(buf); if (n <= 0) break; chunks.put(buf.copyOf(n)) }
                        }
                    }
                } catch (_: Exception) { }
                finally { chunks.put(ByteArray(0)); clips.remove(this) }
            }.start()
        }
        fun cancel() { try { call?.cancel() } catch (_: Exception) {} }
    }

    private suspend fun loop(q: Channel<String>, g: Int) {
        while (true) {
            val first = q.receiveCatching().getOrNull() ?: return      // waits here between replies; ends on stop()
            speaking.value = true
            val o = Out(level).also { out = it }
            try {
                var cur: Clip? = Clip(first)
                while (cur != null) {
                    var next: Clip? = null
                    val prefetch = { if (next == null) next = q.tryReceive().getOrNull()?.let { Clip(it) } }
                    play(cur, o, prefetch)
                    prefetch()
                    if (next == null) {                                 // nothing queued: play out, then a short grace
                        var quietSince = 0L
                        while (next == null) {
                            currentCoroutineContext().ensureActive()
                            o.tick(); prefetch()
                            if (o.bufferedMs() <= 0 || o.stalled()) {
                                if (quietSince == 0L) quietSince = System.currentTimeMillis()
                                else if (System.currentTimeMillis() - quietSince > GRACE_MS) break
                            } else quietSince = 0L
                            delay(20)
                        }
                    }
                    cur = next
                }
            } finally {
                o.close(); if (out === o) out = null
                if (gen == g) { level.value = 0f; speaking.value = false }   // an old, interrupted reply never touches a new one
            }
        }
    }

    private suspend fun play(clip: Clip, o: Out, prefetch: () -> Unit) {
        var filling = false; var target = START_MS; var wroteAny = false
        var firstAt = 0L; var gotMs = 0L
        // Ran dry mid-sentence: if the Mac is making speech slower than real time, refill enough that the rest of the
        // sentence plays through without another pause (length guessed from the text, ~14 characters a second).
        fun refill(): Int {
            val secs = (System.currentTimeMillis() - firstAt).coerceAtLeast(1)
            val speed = (gotMs - 800).coerceAtLeast(0).toDouble() / secs  // speech per second of waiting (the first ~0.8 s piece comes at once)
            val leftMs = (clip.text.length / 14.0 * 1000 - gotMs).coerceAtLeast(0.0)
            val need = if (speed >= 1) 0.0 else leftMs * (1 - speed) * 1.2
            return need.toInt().coerceIn(REFILL_MS, MAX_FILL_MS)
        }
        o.resetCarry()
        while (true) {
            currentCoroutineContext().ensureActive()
            prefetch(); o.tick()
            val b = clip.chunks.poll(20, java.util.concurrent.TimeUnit.MILLISECONDS)
            if (b == null) {                                            // nothing new arrived: running dry?
                if (wroteAny && !filling && o.bufferedMs() < LOW_MS) { o.pause(); filling = true; target = refill() }
                continue
            }
            if (b.isEmpty()) break                                      // end of this sentence
            if (!o.open(clip.rate)) return
            if (!filling && o.bufferedMs() < LOW_MS) { o.pause(); filling = true; target = if (wroteAny) refill() else START_MS }
            if (firstAt == 0L) firstAt = System.currentTimeMillis()
            gotMs += b.size * 500L / clip.rate
            o.write(b)
            wroteAny = true
            if (filling && o.bufferedMs() >= minOf(target.toLong(), o.capMs() * 3 / 4)) { o.play(); filling = false }
        }
        if (filling) o.play()                                           // short sentence: play what there is
    }
}

/** The speaker itself: one AudioTrack, written ahead of what's playing; tracks how much is still waiting to be heard
 *  and drives the ring's level from what is actually playing right now (not what was just downloaded). */
private class Out(private val level: MutableStateFlow<Float>) {
    private var t: AudioTrack? = null
    private var rate = 0
    private var cap = 0L                        // frames the track can hold
    private var written = 0L                    // frames written
    private var playing = false
    private var carry = -1
    private var lastHead = -1L; private var headMovedAt = 0L
    private val marks = ArrayDeque<Pair<Long, Float>>()   // (frame where this level ends, level)
    @Volatile private var dead = false

    /** Opens on the first audio; a different sample rate (never expected) plays out the old track first. */
    fun open(r: Int): Boolean {
        if (dead) return false
        if (t != null && r == rate) return true
        if (t != null) { while (!dead && bufferedMs() > 0 && !stalled()) { tick(); Thread.sleep(20) }; close(); dead = false }
        val tr = try {
            AudioTrack.Builder()
                .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(AudioFormat.Builder().setSampleRate(r).setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setBufferSizeInBytes(r * 2 * (if (android.os.Build.VERSION.SDK_INT >= 31) 6 else 1)).setTransferMode(AudioTrack.MODE_STREAM).build()
        } catch (e: Exception) { return false }
        // Android otherwise waits for the WHOLE 6 s buffer to fill before making a sound; the reserve logic above decides.
        if (android.os.Build.VERSION.SDK_INT >= 31) try { tr.setStartThresholdInFrames(maxOf(1, r / 50)) } catch (_: Exception) {}
        synchronized(this) { if (dead) { tr.release(); return false }; t = tr }
        rate = r; cap = tr.bufferSizeInFrames.toLong(); written = 0; playing = false; marks.clear()
        lastHead = -1; headMovedAt = System.currentTimeMillis()
        return true
    }

    private fun head(): Long = (t?.playbackHeadPosition?.toLong() ?: 0L) and 0xffffffffL
    fun capMs(): Long = if (rate == 0) 0 else cap * 1000 / rate
    fun bufferedMs(): Long = if (t == null || rate == 0) 0 else (written - head()).coerceAtLeast(0) * 1000 / rate

    /** Playing, but the position hasn't moved for a while (some devices stop a few samples short of the end). */
    fun stalled(): Boolean = playing && System.currentTimeMillis() - headMovedAt > 700

    fun resetCarry() { carry = -1 }

    fun write(bytes: ByteArray) {
        var off = 0; var len = bytes.size
        val tr = t ?: return
        if (carry >= 0 && len > 0) { tr.write(byteArrayOf(carry.toByte(), bytes[0]), 0, 2); written++; off = 1; len--; carry = -1 }
        if (len % 2 == 1) { carry = bytes[off + len - 1].toInt() and 0xff; len-- }
        if (len <= 0) return
        var sum = 0.0; var i = off
        while (i + 1 < off + len) { val v = ((bytes[i + 1].toInt() shl 8) or (bytes[i].toInt() and 0xff)).toShort() / 32768.0; sum += v * v; i += 2 }
        val frames = (len / 2).toLong()
        while (!dead && written - head() + frames > cap - rate / 10) {
            if (!playing) play()                                        // full while waiting for the reserve: start now
            tick(); Thread.sleep(20)
        }   // room first, so write() never blocks
        if (dead) return
        if (tr.write(bytes, off, len) < 0) return
        written += frames
        marks.addLast(written to (sqrt(sum / frames) * 6).toFloat().coerceIn(0f, 1f))
    }

    fun play() { if (!playing && !dead) { try { t?.play() } catch (_: Exception) {}; playing = true; headMovedAt = System.currentTimeMillis() } }
    fun pause() { if (playing && !dead) { try { t?.pause() } catch (_: Exception) {}; playing = false } }

    /** Keeps the ring in step with what is being heard. */
    fun tick() {
        if (t == null) return
        val h = head()
        if (h != lastHead) { lastHead = h; headMovedAt = System.currentTimeMillis() }
        while (marks.size > 1 && marks.first().first <= h) marks.removeFirst()
        level.value = if (!playing || bufferedMs() <= 0) 0f else marks.firstOrNull()?.second ?: 0f
    }

    /** Interrupted: silent immediately. */
    fun kill() {
        dead = true
        synchronized(this) { try { t?.pause(); t?.flush() } catch (_: Exception) {} }
    }

    /** Called only after everything written has been heard (or after kill()). */
    fun close() {
        synchronized(this) { try { t?.stop() } catch (_: Exception) {}; try { t?.release() } catch (_: Exception) {}; t = null }
        playing = false; dead = true
    }
}

/** Splits streamed reply text into sentences to speak as soon as each is complete. Long sentences go to the voice in
 *  short phrases: the voice model skips words in long ones (measured: 36 words in one go lost ~20% of the speech). */
class Sentences(private val out: (String) -> Unit) {
    private val buf = StringBuilder()
    fun add(piece: String) {
        buf.append(piece)
        while (true) {
            val m = Regex("""^(.+?[.!?…])(\s+)""", RegexOption.DOT_MATCHES_ALL).find(buf) ?: break
            phrases(m.groupValues[1].trim()).forEach(out); buf.delete(0, m.value.length)
        }
    }
    fun finish() { if (buf.isNotBlank()) phrases(buf.toString().trim()).forEach(out); buf.clear() }

    companion object {
        const val MAX = 90            // characters per piece the voice reads reliably
        fun phrases(s: String): List<String> {
            if (s.length <= MAX) return listOf(s)
            val parts = s.split(Regex("(?<=[,;:—])\\s+|\\s+(?=—)")).filter { it.isNotBlank() }
            val out = mutableListOf<String>(); var cur = ""
            for (p in parts.flatMap { if (it.length > MAX) splitWords(it) else listOf(it) }) {
                cur = if (cur.isEmpty()) p else if (cur.length + 1 + p.length <= MAX) "$cur $p" else { out += cur; p }
            }
            if (cur.isNotEmpty()) out += cur
            return out
        }
        private fun splitWords(s: String): List<String> {
            // no comma to break at: before "and/but/which/who/because/so/then/while" near the middle, else by words
            val w = s.split(" "); val out = mutableListOf<String>(); var cur = mutableListOf<String>()
            for (x in w) {
                if (cur.size >= 8 && x.lowercase() in setOf("and", "but", "which", "who", "because", "so", "then", "while", "with")) {
                    out += cur.joinToString(" "); cur = mutableListOf()
                }
                cur += x
                if (cur.size >= 16) { out += cur.joinToString(" "); cur = mutableListOf() }
            }
            if (cur.isNotEmpty()) out += cur.joinToString(" ")
            return out
        }
    }
    fun reset() = buf.clear()
}
