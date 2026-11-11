package com.echopersona.echo

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

/** Where Show Echo's pictures come from (the camera screen); null when the camera is off. */
interface Frames {
    val camera: String                     // "back" (what the owner sees) or "front" (the owner)
    fun jpeg(maxSide: Int): String?        // base64 JPEG of what the camera sees right now
}

/** One conversation with Echo, shared by the presence screen and Show Echo: listen → speech to text on the Mac →
 *  Echo's reply streamed in, spoken sentence by sentence; tap while he talks to interrupt. */
class Talk(private val link: () -> EchoLink?) {
    val rec = Recorder()
    val speaker = Speaker(link)
    private val sentences = Sentences { speaker.say(it) }
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    var listening by mutableStateOf(false); private set
    var waiting by mutableStateOf(false)
    var interrupted by mutableStateOf(false)
    var you by mutableStateOf("")
    var echo by mutableStateOf("")
    var note by mutableStateOf("")
    var expr by mutableStateOf("neutral")
    var frames: Frames? = null
    var doc by mutableStateOf<Pair<String, String>?>(null)    // "Ask Echo about this": (document id, name)
    var task by mutableStateOf<Pair<String, String>?>(null)   // calls & texts: (call_prep|after_call|text_draft, name)
    var intercept: ((String) -> Boolean)? = null               // "call the loved one" / "text the loved one …" handled on the device
    val errands = androidx.compose.runtime.mutableStateListOf<Errand>()   // prepared by Echo, waiting for Approve
    var onReply: ((String) -> Unit)? = null                   // "Echo joins in" keeps its own transcript
    private var gotDelta = false
    private var toRead: List<Errand> = emptyList()
    var user = "owner"

    /** the owner's wording for handing over an approval (same as core/errands.approve_line on the Mac). */
    fun approveLine(kind: String = "text"): String {
        val (name, pos) = if (user == "loved_one") "the loved one" to "her" else "the owner" to "his"
        return if (kind == "call") "$name, tap Approve on the card that appears and I'll place the call from $pos phone."
        else "$name, tap Approve on the card that appears for the message to send from $pos phone. Here is the text of " +
            "what I'm sending for you to review before approval:"
    }
    private var collecting: Job? = null

    fun attach(lk: EchoLink?) {
        collecting?.cancel()
        lk ?: return
        collecting = scope.launch {
            lk.events.collect { m ->
                when (m.optString("type")) {
                    "state" -> if (m.optString("state") == "thinking") waiting = true else if (m.optString("state") == "idle") waiting = false
                    "delta" -> {
                        if (!gotDelta) { echo = ""; gotDelta = true }
                        echo += m.optString("text"); sentences.add(m.optString("text"))
                        m.optString("expression").takeIf { it.isNotBlank() }?.let { expr = it }
                    }
                    "reply" -> {
                        if (gotDelta) sentences.finish() else { sentences.add(m.optString("text") + " "); sentences.finish() }
                        echo = m.optString("text"); gotDelta = false; waiting = false
                        // The message Echo just read out in quotes IS the text: the card takes those exact words
                        val quoted = Regex("[\"“]([^\"”]{15,})[\"”]").findAll(echo).map { it.groupValues[1].trim() }.lastOrNull()
                        val ti = errands.indexOfFirst { it.kind == "text" }
                        if (quoted != null && ti >= 0) {
                            errands[ti] = errands[ti].copy(body = quoted); toRead = emptyList()
                        }
                        if (toRead.isNotEmpty()) {          // the owner hears the exact words before the Approve box shows
                            val line = toRead.joinToString(" ") { e ->
                                val who = e.who?.let { Phonebook.greetName(it.name).ifBlank { it.name } } ?: e.to
                                "${approveLine()} ${e.body}" }
                            echo = (echo + "\n\n" + line).trim(); speaker.say(line); toRead = emptyList()
                        }
                        onReply?.invoke(echo)
                        m.optString("expression").takeIf { it.isNotBlank() }?.let { expr = it }
                    }
                    "error" -> { note = m.optString("text"); waiting = false }
                    "panel" -> onPanel?.invoke(m)
                    // a photo Echo puts on screen ("Here it is.") / puts away: the side window shows / closes it
                    "photo" -> Regex("([0-9a-f]{16})\\.jpg").find(m.optString("url"))?.let { p ->
                        onPanel?.invoke(JSONObject().put("kind", "photo").put("id", p.groupValues[1])) }
                    "photo_close" -> onPanel?.invoke(JSONObject().put("kind", "photo_close"))
                    "errands" -> {
                        errands.clear(); jsonObjects(m.optJSONArray("items")).forEach { errands.add(Errand.from(it)) }
                        toRead = errands.filter { it.kind == "text" && it.body.isNotBlank() }    // read out after the reply
                    }
                }
            }
        }
    }

    var onAsk: ((String) -> Unit)? = null                       // the side window: weather, calendar, photos
    var onPanel: ((JSONObject) -> Unit)? = null                 // ... and the websites Echo used

    fun send(text: String, voice: Boolean, pictures: List<String> = emptyList(), camera: String = "back") {
        you = text; echo = ""; note = ""; gotDelta = false; waiting = true; sentences.reset()
        onAsk?.invoke(text)
        val m = JSONObject().put("text", text).put("how", if (voice) "voice" else "typed")
        if (pictures.isNotEmpty()) m.put("images", JSONArray(pictures)).put("camera", camera)
        doc?.let { m.put("doc", it.first) }
        task?.let { m.put("task", JSONObject().put("kind", it.first).put("who", it.second)) }
        link()?.sayJson(m)
    }

    /** "Watch what happens…": several pictures over the time he spoke; otherwise one sharp picture at the end. */
    private fun wantsMotion(text: String) = Regex("""\b(watch|happens?|happening|moving|moves|when I (turn|press|push|start|switch|open|pull)|keeps?|sequence)\b""",
        RegexOption.IGNORE_CASE).containsMatchIn(text)

    fun listen(onNoMic: () -> Unit) {
        listening = true; note = ""
        val cam = frames
        val during = mutableListOf<String>()
        val shooter = cam?.let { f -> scope.launch(Dispatchers.Default) {
            while (true) { f.jpeg(512)?.let { synchronized(during) { during += it } }; delay(1000) }
        } }
        rec.record { wav ->
            scope.launch {
                shooter?.cancel()
                if (wav == null) { listening = false; note = "The microphone couldn't start."; onNoMic(); return@launch }
                if (wav.size <= 44) { listening = false; note = "I didn't hear anything. Tap and talk."; return@launch }
                val last = cam?.let { f -> withContext(Dispatchers.Default) { f.jpeg(768) } }   // what's in view as he finishes
                var text = withContext(Dispatchers.IO) { link()?.stt(wav) }?.let { Phonebook.align(it) }   // names as in his contacts
                // Active listening for texts: people pause while they work out the words, so for a text Echo keeps the
                // mic open and adds on what comes next, until a long quiet (or a tap on the ring).
                var rounds = 0
                while (!text.isNullOrBlank() && dictating(text) && rounds < 6 && !stopDictation) {
                    rounds++; you = text; note = "I'm listening… keep going. (Tap the ring when you're done.)"
                    val more = recordOnce(quietLimitMs = 2600, noSpeechMs = 4000) ?: break
                    if (more.size <= 44 || stopDictation && more.size < 16000) break
                    val add = withContext(Dispatchers.IO) { link()?.stt(more) }?.let { Phonebook.align(it) }
                    if (add.isNullOrBlank()) break
                    text = text.trimEnd() + " " + add.trim()
                }
                stopDictation = false; listening = false; note = ""
                waiting = true
                if (text.isNullOrBlank()) { waiting = false; note = "I didn't catch that. Tap and try again."; return@launch }
                if (intercept?.invoke(text) == true) { waiting = false; you = text; echo = ""; return@launch }
                val pics = when {
                    cam == null -> emptyList()
                    wantsMotion(text) -> synchronized(during) { pick(during, 2) } + listOfNotNull(last)
                    else -> listOfNotNull(last)
                }
                send(text, true, pics, cam?.camera ?: "back")
            }
        }
    }

    @Volatile private var stopDictation = false

    /** A text being dictated: "send a text to the loved one saying…", "text Danny that…", "tell her…". */
    private fun dictating(t: String) = Regex("\\b(?:text|message|texts|tell (?:her|him|them)|let (?:her|him|them) know)\\b", RegexOption.IGNORE_CASE)
        .containsMatchIn(t) && !Regex("^(?:what|did|when|who|has|have|why|how|is|was|does)\\b", RegexOption.IGNORE_CASE).containsMatchIn(t.trim())

    private suspend fun recordOnce(quietLimitMs: Int, noSpeechMs: Int): ByteArray? =
        kotlinx.coroutines.suspendCancellableCoroutine { k -> rec.record(quietLimitMs, noSpeechMs) { w -> if (k.isActive) k.resumeWith(Result.success(w)) } }

    private fun pick(xs: List<String>, n: Int): List<String> =
        if (xs.size <= n) xs else (0 until n).map { xs[it * (xs.size - 1) / maxOf(1, n)] }

    fun finishListening() { if (you.isNotBlank() && note.startsWith("I'm listening")) stopDictation = true; rec.finish() }

    fun interrupt() { speaker.stop(); sentences.reset(); interrupted = true; scope.launch { delay(500); interrupted = false } }

    /** Speech to text on the Mac, with who spoke (from the voice: "owner" or "unknown"). */
    fun sttWho(wav: ByteArray): Pair<String, String>? = link()?.sttFull(wav)

    /** "Echo joins in": the recent room conversation goes with the line said to Echo; the Mac keeps none of it. */
    fun sendGroup(text: String, lines: List<Line>, with: String, asker: String) {
        you = text; echo = ""; note = ""; gotDelta = false; waiting = true; sentences.reset()
        val g = JSONObject().put("with", with).put("asker", asker)
            .put("lines", JSONArray(lines.map { JSONObject().put("who", it.who).put("text", it.text) }))
        link()?.sayJson(JSONObject().put("text", text).put("how", "voice").put("group", g))
    }

    /** Typed messages: "text the loved one …" is handled on the device too. */
    fun submit(text: String) { if (intercept?.invoke(text) == true) { you = text; echo = "" } else send(text, false) }

    /** Show Echo's "Look" button: one picture, no words needed. */
    fun look() {
        val f = frames ?: return
        scope.launch {
            val p = withContext(Dispatchers.Default) { f.jpeg(768) } ?: return@launch
            send(if (f.camera == "front") "Look at me." else "Look at this.", false, listOf(p), f.camera)
        }
    }

    fun presence(conn: Conn, speaking: Boolean): Presence = when {
        conn == Conn.RECONNECTING || conn == Conn.CONNECTING -> Presence.RECONNECTING
        conn != Conn.CONNECTED -> Presence.DISCONNECTED
        interrupted -> Presence.INTERRUPTED
        listening -> Presence.LISTENING
        speaking -> Presence.SPEAKING
        waiting -> Presence.THINKING
        else -> Presence.IDLE
    }
}
