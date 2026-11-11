package com.echopersona.echo

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import android.view.HapticFeedbackConstants
import androidx.activity.compose.setContent
import androidx.activity.result.IntentSenderRequest
import androidx.activity.result.contract.ActivityResultContracts
import com.google.mlkit.vision.documentscanner.GmsDocumentScanner
import com.google.mlkit.vision.documentscanner.GmsDocumentScannerOptions
import com.google.mlkit.vision.documentscanner.GmsDocumentScanning
import com.google.mlkit.vision.documentscanner.GmsDocumentScanningResult
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_WEAK
import androidx.biometric.BiometricManager.Authenticators.DEVICE_CREDENTIAL
import androidx.biometric.BiometricPrompt
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.lifecycleScope
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File

private val Cream = Color(0xFFEEE6D6)
private val Muted = Color(0xFFA89F8F)
private val Card = Color(0xFF1A1611)

class MainActivity : FragmentActivity() {
    private var unlocked by mutableStateOf(false)
    private var lockNote by mutableStateOf("")
    private var linked by mutableStateOf<Linked?>(null)
    private var link by mutableStateOf<EchoLink?>(null)
    private var closeKeyboard by mutableStateOf(0)            // bumped when the side window is dismissed
    private val panelState = mutableStateOf<PanelReq?>(null)  // the side window (only Dismiss closes it)
    private var stoppedAt = 0L
    private val perm = registerForActivityResult(ActivityResultContracts.RequestPermission()) { permAnswer?.invoke(it) }
    private var permAnswer: ((Boolean) -> Unit)? = null
    private fun askPermission(p: String, answer: (Boolean) -> Unit) { permAnswer = answer; perm.launch(p) }
    private val scans by lazy { ScanStore(this) }
    private val uploader by lazy { Uploader(scans) { link } }
    private var newScan by mutableStateOf<Triple<java.io.InputStream, java.io.InputStream?, Int>?>(null)
    private val scanLauncher = registerForActivityResult(ActivityResultContracts.StartIntentSenderForResult()) { res ->
        val r = GmsDocumentScanningResult.fromActivityResultIntent(res.data) ?: return@registerForActivityResult
        val pdf = r.pdf ?: return@registerForActivityResult
        newScan = Triple(contentResolver.openInputStream(pdf.uri)!!,
            r.pages?.firstOrNull()?.imageUri?.let { contentResolver.openInputStream(it) }, pdf.pageCount)
    }

    private fun startScan() {
        val opts = GmsDocumentScannerOptions.Builder().setGalleryImportAllowed(true).setPageLimit(50)
            .setResultFormats(GmsDocumentScannerOptions.RESULT_FORMAT_JPEG, GmsDocumentScannerOptions.RESULT_FORMAT_PDF)
            .setScannerMode(GmsDocumentScannerOptions.SCANNER_MODE_FULL).build()
        GmsDocumentScanning.getClient(opts).getStartScanIntent(this)
            .addOnSuccessListener { scanLauncher.launch(IntentSenderRequest.Builder(it).build()) }
            .addOnFailureListener { android.widget.Toast.makeText(this, "The scanner didn't open: ${it.message}", android.widget.Toast.LENGTH_LONG).show() }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        linked = Saved.load(this)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(primary = Gold, background = Ink, surface = Ink)) {
                Box(Modifier.fillMaxSize().background(Brush.verticalGradient(listOf(Color(0xFF17130E), Ink, Color(0xFF080706))))
                    .safeDrawingPadding()) {
                    when {
                        !unlocked -> LockScreen()
                        linked == null -> LinkScreen()
                        else -> Main(linked!!)
                    }
                }
            }
        }
    }

    override fun onStart() {
        super.onStart()
        if (unlocked && stoppedAt > 0 && System.currentTimeMillis() - stoppedAt > 5 * 60_000) unlocked = false
        if (!unlocked) unlock()
        startLink()
    }

    override fun onStop() {
        super.onStop()
        stoppedAt = System.currentTimeMillis()
        link?.stop(); link = null       // nothing listens or stays connected in the background
    }

    private fun startLink() {
        val l = linked ?: return
        if (link == null && unlocked) link = EchoLink(this, l).also { it.start() }
    }

    // ---- unlock: fingerprint/face or the device PIN -------------------------------------------------------------
    private fun unlock() {
        val bm = BiometricManager.from(this)
        if (bm.canAuthenticate(BIOMETRIC_WEAK or DEVICE_CREDENTIAL) != BiometricManager.BIOMETRIC_SUCCESS) {
            lockNote = "This device has no screen lock, so Echo opens without one. Set a PIN in Settings → Security to protect him."
            unlocked = true; startLink(); return
        }
        BiometricPrompt(this, ContextCompat.getMainExecutor(this), object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                unlocked = true; lockNote = ""; startLink()
            }
            override fun onAuthenticationError(errorCode: Int, errString: CharSequence) { lockNote = errString.toString() }
        }).authenticate(BiometricPrompt.PromptInfo.Builder().setTitle("Echo")
            .setSubtitle("Unlock to talk with Echo").setAllowedAuthenticators(BIOMETRIC_WEAK or DEVICE_CREDENTIAL).build())
    }

    @Composable
    private fun LockScreen() {
        Column(Modifier.fillMaxSize().padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center) {
            PresenceRing(Presence.IDLE, 0f, loadFace("neutral"), 260.dp)
            Spacer(Modifier.height(24.dp))
            Big("Echo")
            if (lockNote.isNotBlank()) Small(lockNote)
            Spacer(Modifier.height(16.dp))
            GoldButton("Unlock") { unlock() }
        }
    }

    // ---- linking ------------------------------------------------------------------------------------------------
    private fun deviceName(): String =
        (Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME) ?: Away.deviceName())
            .replace(Regex("[^\\w .'-]"), "").take(40).ifBlank { "device" }

    @Composable
    private fun LinkScreen() {
        val disc = remember { Discovery(this) }
        val found by disc.found.collectAsState()
        var code by remember { mutableStateOf("") }
        var note by remember { mutableStateOf("") }
        var busy by remember { mutableStateOf(false) }
        val scope = rememberCoroutineScope()
        LaunchedEffect(Unit) { disc.start() }

        fun waitForMac(p: Pairing, pinFromQr: ByteArray?) = scope.launch {
            while (true) {
                delay(2000)
                val s = try { withContext(Dispatchers.IO) { p.status() } } catch (e: Exception) { continue }
                when (s.optString("status")) {
                    "approved" -> {
                        val pin = pinFromQr ?: p.trust.seen!!
                        val l = Linked(s.getString("device_id"), s.getString("user"), s.getString("mac_id"),
                            s.optString("name", "Echo"), b64(pin), jsonList(s.optJSONArray("lan")),
                            s.optInt("port", 8771), s.optString("ts"), s.optString("ti"))
                        Saved.save(this@MainActivity, l); disc.stop()
                        window.decorView.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                        linked = l; startLink(); return@launch
                    }
                    "denied" -> { code = ""; busy = false; note = "The Mac said no. Try again if that was a mistake."; return@launch }
                    "expired" -> { code = ""; busy = false; note = "That took too long. Try again."; return@launch }
                }
            }
        }

        fun linkFound(f: Discovery.Found) = scope.launch {
            busy = true; note = "Asking the Mac…"
            val p = Pairing("https://${f.host}:${f.port}", PinTrust(null))
            try {
                code = withContext(Dispatchers.IO) { p.start("discover", "", deviceName()) }; note = ""
                waitForMac(p, null)
            } catch (e: Exception) { busy = false; note = "Couldn't reach Echo: ${e.message}" }
        }

        fun scanQr() {
            val opts = GmsBarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build()
            GmsBarcodeScanning.getClient(this, opts).startScan().addOnSuccessListener { b ->
                scope.launch {
                    busy = true; note = "Finding Echo…"
                    try {
                        val q = JSONObject(b.rawValue ?: "")
                        if (q.optString("t") != "echolink") throw Exception("that isn't Echo's code")
                        val pin = unb64(q.getString("fp"))
                        val port = q.optInt("p", 8771)
                        val hosts = jsonList(q.optJSONArray("h")) + listOf(q.optString("ts"), q.optString("ti")).filter { it.isNotBlank() }
                        val (p, c) = withContext(Dispatchers.IO) {
                            var last: Exception? = null
                            for (h in hosts) {                         // home first, then Tailscale (works anywhere)
                                try {
                                    val p = Pairing("https://$h:$port", PinTrust(pin))
                                    return@withContext p to p.start("qr", q.getString("k"), deviceName())
                                } catch (e: HttpError) { throw e } catch (e: Exception) { last = e }
                            }
                            throw Exception(if (Away.vpnOn(this@MainActivity)) "the Mac didn't answer" else
                                "not on the home Wi-Fi and Tailscale is off", last)
                        }
                        code = c; note = ""; waitForMac(p, pin)
                    } catch (e: Exception) { busy = false; note = "Couldn't link: ${e.message}" }
                }
            }.addOnFailureListener { note = "The scanner didn't open: ${it.message}" }
        }

        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally) {
            Spacer(Modifier.height(24.dp))
            PresenceRing(Presence.IDLE, 0f, null, 200.dp)
            Big("Link this device to Echo")
            Small("Once. After that it connects by itself, at home or away.")
            Spacer(Modifier.height(20.dp))
            if (code.isNotBlank()) {
                CardBox {
                    Small("Check that the Mac shows this same number:")
                    Text(code, color = GoldLight, fontSize = 52.sp, letterSpacing = 8.sp, fontWeight = FontWeight.Light)
                    Small("Then on the Mac choose the owner or the loved one. Waiting…")
                }
            } else {
                GoldButton("Scan the QR code on the Mac", enabled = !busy) { scanQr() }
                Small("On the Mac: open Echo Link (http://127.0.0.1:8772) → Show QR code.")
                Spacer(Modifier.height(18.dp))
                CardBox {
                    Text("Found on this Wi-Fi", color = Gold, fontSize = 18.sp)
                    if (found.isEmpty()) Small("Looking… (works on the home Wi-Fi)")
                    found.forEach { f ->
                        Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                            Text(f.name, color = Cream, fontSize = 18.sp, modifier = Modifier.weight(1f))
                            GoldButton("Link", enabled = !busy) { linkFound(f) }
                        }
                    }
                }
            }
            if (note.isNotBlank()) { Spacer(Modifier.height(12.dp)); Small(note) }
            Spacer(Modifier.height(18.dp))
            AwayCard()
        }
    }

    // ---- main: presence + menu ------------------------------------------------------------------------------------
    @Composable
    private fun Main(l: Linked) {
        var page by remember { mutableStateOf("Echo") }
        var menu by remember { mutableStateOf(false) }
        val conn by (link?.state ?: remember { kotlinx.coroutines.flow.MutableStateFlow(Conn.CONNECTING) }).collectAsState()
        if (conn == Conn.REMOVED) { Removed(); return }
        val view = LocalView.current
        val talk = remember { Talk { link } }
        LaunchedEffect(link) { talk.attach(link) }
        var face by remember { mutableStateOf(loadFace("neutral")) }
        val speaking by talk.speaker.speaking.collectAsState()
        LaunchedEffect(talk.expr, conn) {           // his face follows the feeling of the reply (kept for when away)
            if (conn == Conn.CONNECTED) withContext(Dispatchers.IO) {
                listOf("neutral", talk.expr).distinct().forEach { e ->
                    if (!faceFile(e).exists()) link?.face(e)?.let { faceFile(e).writeBytes(it) }
                }
            }
            loadFace(talk.expr)?.let { face = it }
        }
        LaunchedEffect(speaking) { if (!speaking && talk.expr != "neutral") { delay(2500); talk.expr = "neutral" } }
        LaunchedEffect(conn) { if (conn == Conn.CONNECTED && l.user == "owner") uploader.kick() }
        fun listen() {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                askPermission(Manifest.permission.RECORD_AUDIO) { ok -> if (ok) listen() else talk.note =
                    "Echo needs the microphone to hear you. Allow it in Settings → Apps → Echo → Permissions, or type instead." }
                return
            }
            view.performHapticFeedback(HapticFeedbackConstants.CONTEXT_CLICK)
            talk.listen { }
        }
        fun tapRing() {
            when {
                conn != Conn.CONNECTED -> link?.reconnectNow()
                talk.listening -> talk.finishListening()
                speaking -> { talk.interrupt(); view.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS); listen() }
                else -> listen()
            }
        }
        fun open(p: String) {
            if (p == "Talk together" && ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                askPermission(Manifest.permission.RECORD_AUDIO) { ok -> if (ok) page = p }
            } else if (p == "Show Echo" && ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                askPermission(Manifest.permission.CAMERA) { ok -> if (ok) page = p else talk.note =
                    "Show Echo needs the camera. Allow it in Settings → Apps → Echo → Permissions." }
            } else page = p
        }
        var card by remember { mutableStateOf<Contact?>(null) }
        var textCard by remember { mutableStateOf<Triple<Contact?, List<Contact>, String>?>(null) }
        var taskContact by remember { mutableStateOf<Contact?>(null) }
        var togetherWith by remember { mutableStateOf("") }
        var echoCall by remember { mutableStateOf<Pair<Contact, String>?>(null) }
        var lookup by remember { mutableStateOf<Pair<String, String>?>(null) }
        var panelReq by panelState                  // kept by the activity: survives the app re-locking
        var hasCalendar by remember { mutableStateOf(ContextCompat.checkSelfPermission(this, Manifest.permission.READ_CALENDAR) == PackageManager.PERMISSION_GRANTED) }
        LaunchedEffect(Unit) {
            talk.onAsk = { said -> Panels.forAsk(said)?.let { panelReq = it } }
            talk.onPanel = { m ->
                when (m.optString("kind")) {
                    "photo" -> panelReq = PanelReq("photo", m.optString("id"))
                    "photo_close" -> if (panelReq?.kind == "photo") panelReq = null
                    else -> if (panelReq == null || panelReq?.kind == "web")
                        panelReq = PanelReq("web", items = m.optJSONArray("items"), title = m.optString("title"))
                }
            }
        }
        var hasContacts by remember { mutableStateOf(ContextCompat.checkSelfPermission(this, Manifest.permission.READ_CONTACTS) == PackageManager.PERMISSION_GRANTED) }
        LaunchedEffect(hasContacts) { if (hasContacts && l.user == "owner") withContext(Dispatchers.IO) { Phonebook.load(this@MainActivity) } }
        LaunchedEffect(conn) { if (conn == Conn.CONNECTED && l.user == "owner") withContext(Dispatchers.IO) { link?.let { lk -> MacLine.contacts(lk)?.let { Phonebook.setMac(it) } } } }
        LaunchedEffect(Unit) {
            // the owner: Echo says it first (out loud, words on screen); the approval box comes after he's finished
            fun afterSpeech(block: () -> Unit) = lifecycleScope.launch {
                delay(500)
                while (talk.speaker.speaking.value || talk.waiting) delay(150)
                block()
            }
            fun announce(line: String, then: () -> Unit) { talk.echo = line; talk.note = ""; talk.speaker.say(line); afterSpeech(then) }
            talk.user = l.user
            talk.intercept = if (l.user != "owner") null else { said ->
                Phonebook.parse(said, Phonebook.cache)?.let { a ->
                    page = "Echo"
                    val one = a.matches.singleOrNull()
                    val first = one?.let { Phonebook.greetName(it.name).ifBlank { it.name } } ?: ""
                    when {
                        a.kind == "echocall" && one != null -> announce("Okay, I'll call $first for you. Here's how I'll start; " +
                            "check it, then tap Approve.") { echoCall = one to a.body }
                        a.kind == "echocall" -> announce("Which one should I call?") { textCard = Triple(null, a.matches, "call") }
                        // one person, and (for a text) the words: Echo's card, Approve sends it / places the call
                        one != null && (a.kind == "call" || a.body.isNotBlank()) -> {
                            val e = Errand(a.kind, one.name, a.body, "", "", "").resolve()
                            talk.errands.clear()
                            announce(if (a.kind == "call") "Ready to call $first. ${talk.approveLine("call")}"
                                     else "${talk.approveLine()} ${e.body}") { talk.errands.add(e) }
                        }
                        a.kind == "show" -> announce(if (one != null) "Here's ${one.name}." else "I found a few. Which one?") {
                            if (one != null) card = one else textCard = Triple(null, a.matches, "call") }
                        a.kind == "call" -> announce("Which one should I call?") { textCard = Triple(null, a.matches, "call") }
                        one != null -> announce("What would you like to say to $first?") { textCard = Triple(one, a.matches, a.body) }
                        else -> announce("Which one do you mean?") { textCard = Triple(one, a.matches, a.body) }
                    }
                    true
                } ?: Phonebook.callTarget(said)?.let { (who, _) ->
                    // not anyone in the contacts: Echo looks the number up online
                    if (Phonebook.find(who).isNotEmpty()) return@let false
                    page = "Echo"
                    announce("$who isn't in your contacts, so I'll look the number up online.") { lookup = who to said }
                    true
                } ?: false
            }
        }
        fun withEcho(kind: String, c: Contact, opening: String?) {
            card = null; textCard = null; taskContact = c; talk.doc = null
            talk.task = kind to c.name; page = "Echo"
            if (opening != null) talk.send(opening, false) else talk.note = when (kind) {
                "after_call" -> "Tap the ring and tell Echo how the call with ${c.name} went."
                else -> "Tap the ring and tell Echo what you want to say to ${c.name}." }
        }
        val calls: @Composable () -> Unit = {
            card?.let { c -> ContactCard(c, { card = null }, { withEcho("call_prep", c, "Help me get ready to call ${c.name}.") },
                { withEcho("after_call", c, null) }, { withEcho("text_draft", c, null) }, { card = null; textCard = Triple(c, listOf(c), "") },
                { card = null; togetherWith = c.name; page = "Talk together" }, link) }
            textCard?.let { (c, choices, body) -> TextCard(c, choices, if (body == "call") "" else body, { textCard = null },
                { x -> if (body == "call") { textCard = null; card = x } else textCard = Triple(x, listOf(x), body) },
                { x, b -> withEcho("text_draft", x, if (b.isBlank()) null else "Write this for me: $b") }, link) }
        }
        fun findContact(name: String): List<Contact> = Phonebook.find(name)
        fun approve(e: Errand) {
            talk.errands.remove(e)
            when (e.kind) {
                "text", "call" -> {
                    if (e.who != null && e.number.isNotBlank() && (e.kind == "call" || e.body.isNotBlank())) {
                        talk.note = if (e.kind == "call") "Calling ${e.who.name}…" else "Sending to ${e.who.name}…"
                        lifecycleScope.launch {
                            val err = withContext(Dispatchers.IO) {
                                if (e.kind == "call") MacLine.call(link, e.who.name, e.number) else MacLine.text(link, e.who.name, e.number, e.body) }
                            talk.note = err ?: if (e.kind == "call") "Calling ${e.who.name} from your iPhone." else "Sent to ${e.who.name}."
                            if (err == null) Phonebook.remember(this@MainActivity, e.who.name, if (e.kind == "call") "call (iPhone)" else "text (iPhone)")
                        }
                        return
                    }
                    val hits = findContact(e.to)
                    when {
                        hits.isEmpty() && e.kind == "call" -> lookup = e.to to "Call ${e.to}"
                        hits.isEmpty() -> talk.note = "There's nobody called ${e.to} in your contacts."
                        e.kind == "call" && hits.size == 1 -> card = hits[0]
                        e.kind == "call" -> textCard = Triple(null, hits, "call")
                        else -> textCard = Triple(hits.singleOrNull(), hits, e.body)
                    }
                }
                "note" -> lifecycleScope.launch(Dispatchers.IO) { try { link?.let { lk -> lk.http.postJson("${lk.base}/v1/calls/notes",
                    JSONObject().put("who", "Note").put("text", e.body), lk.session) }
                    withContext(Dispatchers.Main) { talk.note = "Noted." } } catch (x: Exception) { withContext(Dispatchers.Main) { talk.note = "Couldn't save the note right now." } } }
                else -> {
                    val email = if (e.kind == "email") findContact(e.to).firstOrNull()?.emails?.firstOrNull() else null
                    val i = ErrandRunner.intentFor(this@MainActivity, e, email)
                    try { startActivity(i) } catch (x: Exception) { talk.note = "No app on this tablet can do that one." }
                }
            }
        }
        val taskButtons: @Composable () -> Unit = {
            val speakingNow by talk.speaker.speaking.collectAsState()
            if (!speakingNow && !talk.waiting) ErrandCards(talk.errands.toList(), { approve(it) }, { talk.errands.remove(it) })
            val t = talk.task
            if (t != null && talk.echo.isNotBlank() && !talk.waiting) Row(Modifier.padding(top = 8.dp)) {
                if (t.first == "text_draft") GoldButton("Use this text") { textCard = Triple(taskContact ?: Phonebook.cache.firstOrNull { it.name == t.second },
                    listOfNotNull(taskContact), talk.echo.trim()); talk.task = null }
                if (t.first == "after_call") GoldButton("Save these notes") {
                    val who = t.second; val text = talk.echo
                    lifecycleScope.launch(Dispatchers.IO) { try { link?.let { lk -> lk.http.postJson("${lk.base}/v1/calls/notes",
                        JSONObject().put("who", who).put("text", text), lk.session) }
                        withContext(Dispatchers.Main) { talk.note = "Saved with your calls."; talk.task = null }
                    } catch (e: Exception) { withContext(Dispatchers.Main) { talk.note = "Couldn't save the notes right now; try again when Echo can be reached." } } }
                }
            }
        }
        SideEffect { EchoSaid.text = talk.echo }
        echoCall?.let { (c, req) -> EchoCallScreen(link, c, req, business = c.id <= -900L) { echoCall = null }; return }
        lookup?.let { (what, req) -> LookupScreen(link, what, req, { c, r -> lookup = null; echoCall = c to r }) { lookup = null }; return }
        if (page == "Talk together" && l.user == "owner") {
            TogetherScreen(talk, conn, face, togetherWith) { togetherWith = ""; page = "Echo" }
            return
        }
        if (page == "Show Echo" && l.user == "owner") {
            ShowEchoScreen(talk, conn, face, { tapRing() }) { page = "Echo" }
            return
        }
        Column(Modifier.fillMaxSize()) {
            Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("☰", color = Gold, fontSize = 28.sp, modifier = Modifier.clip(CircleShape).clickable { menu = !menu }.padding(8.dp))
                Text(if (page == "Echo") "" else page, color = Cream, fontSize = 20.sp, modifier = Modifier.weight(1f).padding(start = 8.dp))
                StatusChip(conn) { page = "System" }
            }
            Box(Modifier.weight(1f)) {
                when (page) {
                    "Echo" -> Row(Modifier.fillMaxSize()) {
                        Box(Modifier.weight(1f).fillMaxHeight()) { PresenceScreen(l, conn, talk, face, { tapRing() }, taskButtons) }
                        panelReq?.let { r ->      // the side window, to the right of Echo
                            SidePanel(link, r, hasCalendar, {
                                askPermission(Manifest.permission.READ_CALENDAR) { ok -> hasCalendar = ok }
                            }, {
                                panelReq = null
                                closeKeyboard++                     // and the keyboard closes, like "Hide keyboard"
                                // the owner: once the window is dismissed, Echo's words go too (after he's done talking)
                                lifecycleScope.launch {
                                    while (talk.speaker.speaking.value || talk.waiting) delay(200)
                                    if (panelReq == null && talk.errands.isEmpty()) { talk.you = ""; talk.echo = ""; talk.note = "" }
                                }
                            }, Modifier.fillMaxWidth(0.42f))
                        }
                    }
                    "Calls" -> if (l.user == "owner") CallsScreen(link, conn, hasContacts, { askPermission(Manifest.permission.READ_CONTACTS) { hasContacts = it } }) { card = it } else Coming(page)
                    "System" -> SystemScreen(l, conn)
                    "Scanner" -> if (l.user == "owner") ScannerScreen(scans, uploader, link, conn, { startScan() }, newScan,
                        { newScan = null }) { id, name -> talk.doc = id to name; page = "Echo" } else Coming(page)
                    else -> Coming(page)
                }
                if (menu) Menu(l) { open(it); menu = false }
                calls()
            }
        }
    }

    @Composable
    private fun Menu(l: Linked, pick: (String) -> Unit) {
        val items = listOf("Echo", "Memories", "Echo's World", "Discoveries", "Notifications", "System") +
            (if (l.user == "loved_one") emptyList() else listOf("Show Echo", "Scanner", "Calls", "Talk together"))
        Column(Modifier.fillMaxSize().background(Ink.copy(alpha = 0.96f)).padding(24.dp)) {
            items.forEach { Text(it, color = if (it == "Echo") Gold else Cream, fontSize = 22.sp,
                modifier = Modifier.fillMaxWidth().clickable { pick(it) }.padding(vertical = 14.dp)) }
        }
    }

    @Composable
    private fun Coming(what: String) {
        Column(Modifier.fillMaxSize().padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center) {
            Big(what); Small("Coming in a later step.")
        }
    }

    @Composable
    private fun StatusChip(c: Conn, onClick: () -> Unit) {
        val route by (link?.route ?: remember { kotlinx.coroutines.flow.MutableStateFlow("") }).collectAsState()
        val (txt, col) = when (c) {
            Conn.CONNECTED -> (if (route == "Tailscale") "Connected · away" else "Connected") to Color(0xFF8BC48A)
            Conn.CONNECTING -> "Connecting…" to Muted
            Conn.RECONNECTING -> "Reconnecting…" to Color(0xFFE0B060)
            Conn.OFFLINE -> "Can't reach Echo" to Color(0xFFE08A70)
            Conn.REMOVED -> "Removed" to Color(0xFFE08A70)
        }
        Text("● $txt", color = col, fontSize = 15.sp, modifier = Modifier.clip(RoundedCornerShape(20.dp))
            .border(1.dp, col.copy(alpha = 0.4f), RoundedCornerShape(20.dp)).clickable { onClick() }
            .padding(horizontal = 12.dp, vertical = 6.dp))
    }

    @Composable
    private fun PresenceScreen(l: Linked, conn: Conn, talk: Talk, face: ImageBitmap?, tapRing: () -> Unit,
                               extra: @Composable () -> Unit = {}) {
        var showText by remember { mutableStateOf(true) }
        var typing by remember { mutableStateOf(false) }
        val keyboard = androidx.compose.ui.platform.LocalSoftwareKeyboardController.current
        val focus = androidx.compose.ui.platform.LocalFocusManager.current
        LaunchedEffect(closeKeyboard) { if (closeKeyboard > 0) { typing = false; focus.clearFocus(); keyboard?.hide() } }
        var typed by remember { mutableStateOf("") }
        val speaking by talk.speaker.speaking.collectAsState()
        val micLevel by talk.rec.level.collectAsState()
        val voiceLevel by talk.speaker.level.collectAsState()
        val presence = talk.presence(conn, speaking)
        val hint = when (presence) {
            Presence.IDLE -> "Tap to talk"
            Presence.LISTENING -> "Listening… tap when you're done"
            Presence.THINKING -> "Thinking…"
            Presence.SPEAKING -> "Tap to interrupt"
            Presence.INTERRUPTED -> "Go ahead"
            Presence.RECONNECTING -> "Finding Echo…"
            Presence.DISCONNECTED -> "Can't reach Echo right now. Tap to try again."
        }
        BoxWithConstraints(Modifier.fillMaxSize()) {
            val ring = minOf(maxWidth * 0.9f, maxHeight * 0.55f, 520.dp)
            Column(Modifier.fillMaxSize().padding(horizontal = 20.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                Text(if (l.user == "loved_one") "Hi the loved one" else "Hi the owner", color = Muted, fontSize = 18.sp)
                Spacer(Modifier.weight(0.4f))
                Box(Modifier.clip(CircleShape).clickable(remember { MutableInteractionSource() }, null) { tapRing() }) {
                    PresenceRing(presence, if (talk.listening) micLevel else voiceLevel, face, ring)
                }
                Text(hint, color = Muted, fontSize = 17.sp, modifier = Modifier.padding(top = 6.dp))
                Spacer(Modifier.weight(0.3f))
                talk.doc?.let { d -> Row(Modifier.clip(RoundedCornerShape(20.dp)).background(Card).border(1.dp, Gold.copy(alpha = 0.5f), RoundedCornerShape(20.dp))
                    .padding(horizontal = 14.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text("Asking about: ${d.second}", color = GoldLight, fontSize = 16.sp)
                    Text("  ✕", color = Muted, fontSize = 18.sp, modifier = Modifier.clickable { talk.doc = null })
                } }
                talk.task?.let { t -> Row(Modifier.padding(top = 6.dp).clip(RoundedCornerShape(20.dp)).background(Card)
                    .border(1.dp, Gold.copy(alpha = 0.5f), RoundedCornerShape(20.dp)).padding(horizontal = 14.dp, vertical = 6.dp),
                    verticalAlignment = Alignment.CenterVertically) {
                    Text(when (t.first) { "call_prep" -> "Getting ready to call ${t.second}"; "after_call" -> "Notes after the call with ${t.second}"
                        else -> "Writing a text to ${t.second}" }, color = GoldLight, fontSize = 16.sp)
                    Text("  ✕", color = Muted, fontSize = 18.sp, modifier = Modifier.clickable { talk.task = null })
                } }
                if (showText) Column(Modifier.widthIn(max = 640.dp).fillMaxWidth().weight(0.9f).verticalScroll(rememberScrollState())) {
                    if (talk.you.isNotBlank()) Text(talk.you, color = Muted, fontSize = 17.sp, modifier = Modifier.padding(bottom = 8.dp))
                    if (talk.echo.isNotBlank()) Text(talk.echo, color = Cream, fontSize = 20.sp, lineHeight = 28.sp)
                    if (talk.note.isNotBlank()) Text(talk.note, color = Color(0xFFE0B060), fontSize = 16.sp, modifier = Modifier.padding(top = 8.dp))
                    extra()                                  // approval boxes come after (below) Echo's words
                } else { Spacer(Modifier.weight(0.9f)); extra() }
                if (typing) Row(Modifier.widthIn(max = 640.dp).fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    OutlinedTextField(typed, { typed = it }, Modifier.weight(1f), placeholder = { Text("Type to Echo") },
                        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                        keyboardActions = KeyboardActions(onSend = { if (typed.isNotBlank()) { talk.submit(typed.trim()); typed = "" } }),
                        colors = OutlinedTextFieldDefaults.colors(focusedTextColor = Cream, unfocusedTextColor = Cream,
                            focusedBorderColor = Gold, unfocusedBorderColor = Color(0xFF3A3020)))
                    TextButton({ if (typed.isNotBlank()) { talk.submit(typed.trim()); typed = "" } }) { Text("Send", color = Gold) }
                }
                Row(Modifier.padding(vertical = 8.dp)) {
                    TextButton({ typing = !typing }) { Text(if (typing) "Hide keyboard" else "Type", color = Gold) }
                    TextButton({ showText = !showText }) { Text(if (showText) "Hide words" else "Show words", color = Gold) }
                }
            }
        }
    }

    @Composable
    private fun SystemScreen(l: Linked, conn: Conn) {
        val route by (link?.route ?: remember { kotlinx.coroutines.flow.MutableStateFlow("") }).collectAsState()
        val last by (link?.lastConnected ?: remember { kotlinx.coroutines.flow.MutableStateFlow(0L) }).collectAsState()
        var confirm by remember { mutableStateOf(false) }
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp)) {
            CardBox {
                Row2("Echo", l.macName)
                Row2("This device is", if (l.user == "loved_one") "the loved one's" else "the owner's")
                Row2("Connection", when (conn) { Conn.CONNECTED -> "Connected"; Conn.OFFLINE -> "Can't reach Echo"
                    Conn.REMOVED -> "Removed on the Mac"; else -> "Reconnecting…" })
                Row2("Way", when (route) { "Tailscale" -> "Away from home (Tailscale)"; "" -> "—"; else -> "Home Wi-Fi (direct)" })
                Row2("Last connected", if (last == 0L) "not yet" else android.text.format.DateFormat.format("MMM d, h:mm a", last).toString())
                if (lockNote.isNotBlank()) Small(lockNote)
                GoldButton("Reconnect now") { link?.reconnectNow() }
            }
            Spacer(Modifier.height(14.dp))
            AwayCard()
            Spacer(Modifier.height(14.dp))
            CardBox {
                Small("Unlinking means this device can't reach Echo until it is linked again with the QR code or the Wi-Fi list.")
                if (!confirm) TextButton({ confirm = true }) { Text("Unlink this device", color = Color(0xFFE08A70)) }
                else GoldButton("Yes, unlink") { unlink() }
            }
        }
    }

    @Composable
    private fun Removed() {
        Column(Modifier.fillMaxSize().padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center) {
            Big("This device was removed on the Mac")
            Small("Link it again to talk with Echo.")
            GoldButton("Link again") { unlink() }
        }
    }

    private fun unlink() {
        link?.stop(); link = null
        Saved.forget(this); Keys.reset()
        faceDir().listFiles()?.forEach { it.delete() }
        linked = null
    }

    /** Everything needed to reach Echo when away from home, with one button for each missing piece. */
    @Composable
    private fun AwayCard() {
        var tick by remember { mutableStateOf(0) }
        LaunchedEffect(Unit) { while (true) { delay(3000); tick++ } }
        val inst = remember(tick) { Away.installed(this) }
        val on = remember(tick) { Away.vpnOn(this) }
        CardBox {
            Text(if (inst && on) "✓ Echo can be reached away from home" else "Reach Echo away from home",
                color = if (inst && on) Color(0xFF8BC48A) else Gold, fontSize = 18.sp)
            Small("Away from the home Wi-Fi, Echo is reached through Tailscale (private and encrypted).")
            Step(inst, "1. Install Tailscale") {
                try { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("market://details?id=${Away.TAILSCALE}"))) }
                catch (e: Exception) { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("https://play.google.com/store/apps/details?id=${Away.TAILSCALE}"))) }
            }
            Step(on, "2. Open Tailscale and sign in (the owner's account)") {
                packageManager.getLaunchIntentForPackage(Away.TAILSCALE)?.let { startActivity(it) }
            }
            Step(null, "3. Keep it always on: Tailscale → Always-on VPN") {
                startActivity(Intent(Settings.ACTION_VPN_SETTINGS))
            }
        }
    }

    @Composable
    private fun Step(done: Boolean?, label: String, go: () -> Unit) {
        Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(if (done == true) "✓" else "•", color = if (done == true) Color(0xFF8BC48A) else Muted, fontSize = 20.sp,
                modifier = Modifier.padding(end = 10.dp))
            Text(label, color = Cream, fontSize = 17.sp, modifier = Modifier.weight(1f))
            if (done != true) TextButton(go) { Text(if (done == null) "Open" else "Do it", color = Gold) }
        }
    }

    // ---- face pictures (kept on the device so the ring shows him even when away) --------------------------------
    private fun faceDir() = File(filesDir, "face").apply { mkdirs() }
    private fun faceFile(e: String) = File(faceDir(), "$e.jpg")
    private fun loadFace(e: String): ImageBitmap? = try {
        faceFile(e).takeIf { it.exists() }?.let { BitmapFactory.decodeFile(it.path)?.asImageBitmap() }
    } catch (_: Exception) { null }
}

@Composable private fun Big(t: String) = Text(t, color = Cream, fontSize = 26.sp, textAlign = TextAlign.Center,
    modifier = Modifier.padding(top = 12.dp))
@Composable private fun Small(t: String) = Text(t, color = Muted, fontSize = 16.sp, textAlign = TextAlign.Center,
    modifier = Modifier.padding(top = 6.dp))
@Composable private fun Row2(a: String, b: String) = Row(Modifier.fillMaxWidth().padding(vertical = 5.dp)) {
    Text(a, color = Muted, fontSize = 16.sp, modifier = Modifier.weight(1f)); Text(b, color = Cream, fontSize = 16.sp)
}
@Composable private fun CardBox(content: @Composable () -> Unit) = Column(
    Modifier.widthIn(max = 640.dp).fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(Card)
        .border(1.dp, Color(0xFF3A3020), RoundedCornerShape(16.dp)).padding(16.dp),
    horizontalAlignment = Alignment.CenterHorizontally) { content() }
@Composable private fun GoldButton(t: String, enabled: Boolean = true, onClick: () -> Unit) = Button(onClick, enabled = enabled,
    modifier = Modifier.padding(top = 8.dp).height(52.dp), shape = RoundedCornerShape(14.dp),
    colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Ink)) { Text(t, fontSize = 17.sp) }
