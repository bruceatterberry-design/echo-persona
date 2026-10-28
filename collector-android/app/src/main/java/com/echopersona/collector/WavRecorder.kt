package com.echopersona.collector

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import java.io.File
import java.io.RandomAccessFile
import kotlin.concurrent.thread
import kotlin.math.abs

/** Lossless voice recording: 48 kHz, 16-bit, mono WAV (best for voice cloning). */
class WavRecorder(private val out: File) {
    private val rate = 48000
    @Volatile private var running = false
    @Volatile var level = 0f   // 0..1 peak of the last buffer, for the meter
        private set
    private var worker: Thread? = null
    var startedAt = 0L
        private set

    @SuppressLint("MissingPermission")  // checked by the caller
    fun start() {
        val min = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = AudioRecord(MediaRecorder.AudioSource.MIC, rate, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT, maxOf(min, rate / 5))
        running = true
        startedAt = System.currentTimeMillis()
        worker = thread(name = "wav") {
            RandomAccessFile(out, "rw").use { f ->
                f.setLength(0); f.write(ByteArray(44))   // header written at the end
                val buf = ShortArray(rate / 20); val bytes = ByteArray(buf.size * 2)
                var total = 0L
                rec.startRecording()
                while (running) {
                    val n = rec.read(buf, 0, buf.size)
                    if (n <= 0) continue
                    var peak = 0
                    for (i in 0 until n) {
                        val s = buf[i].toInt(); peak = maxOf(peak, abs(s))
                        bytes[2 * i] = (s and 0xff).toByte(); bytes[2 * i + 1] = ((s shr 8) and 0xff).toByte()
                    }
                    level = peak / 32768f
                    f.write(bytes, 0, n * 2); total += n * 2
                }
                rec.stop(); rec.release()
                f.seek(0); f.write(header(total))
            }
        }
    }

    /** Stops and returns the duration in ms. */
    fun stop(): Long {
        running = false
        worker?.join()
        return System.currentTimeMillis() - startedAt
    }

    private fun header(dataLen: Long): ByteArray {
        val b = java.nio.ByteBuffer.allocate(44).order(java.nio.ByteOrder.LITTLE_ENDIAN)
        b.put("RIFF".toByteArray()).putInt((36 + dataLen).toInt()).put("WAVE".toByteArray())
        b.put("fmt ".toByteArray()).putInt(16).putShort(1).putShort(1).putInt(rate).putInt(rate * 2).putShort(2).putShort(16)
        b.put("data".toByteArray()).putInt(dataLen.toInt())
        return b.array()
    }
}
