package com.echopersona.echo

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Build
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.selects.select
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.security.cert.X509Certificate
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

private val JSON = "application/json".toMediaType()

/** TLS that trusts exactly one certificate: the Mac's, by its SHA-256 fingerprint. With pin == null (first contact
 *  through network discovery) any certificate is accepted but remembered in [seen], and the 6-digit number check
 *  decides whether to trust it. */
class PinTrust(private val pin: ByteArray?) : X509TrustManager {
    @Volatile var seen: ByteArray? = null
    override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) = throw java.security.cert.CertificateException()
    override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
        val fp = sha256(chain[0].encoded)
        seen = fp
        if (pin != null && !fp.contentEquals(pin)) throw java.security.cert.CertificateException("not Echo's certificate")
    }
    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
}

fun pinnedClient(trust: PinTrust, readSeconds: Long = 30): OkHttpClient {
    val ssl = SSLContext.getInstance("TLS").apply { init(null, arrayOf(trust), null) }
    return OkHttpClient.Builder()
        .sslSocketFactory(ssl.socketFactory, trust)
        .hostnameVerifier { _, _ -> true }               // the pin is the trust, at any address
        .connectTimeout(4, TimeUnit.SECONDS).readTimeout(readSeconds, TimeUnit.SECONDS)
        .pingInterval(15, TimeUnit.SECONDS)               // heartbeat on the conversation
        .build()
}

fun OkHttpClient.getJson(url: String, token: String? = null): JSONObject = newCall(
    Request.Builder().url(url).apply { token?.let { header("Authorization", "Bearer $it") } }.build()
).execute().use { r -> body(r) }

fun OkHttpClient.postJson(url: String, obj: JSONObject, token: String? = null): JSONObject = newCall(
    Request.Builder().url(url).post(obj.toString().toRequestBody(JSON))
        .apply { token?.let { header("Authorization", "Bearer $it") } }.build()
).execute().use { r -> body(r) }

class HttpError(val code: Int, msg: String) : Exception(msg)

private fun body(r: Response): JSONObject {
    val t = r.body?.string() ?: "{}"
    val j = try { JSONObject(t) } catch (e: Exception) { JSONObject() }
    if (!r.isSuccessful) throw HttpError(r.code, j.optString("error", "Echo said no (${r.code})"))
    return j
}

fun jsonList(a: JSONArray?): List<String> = (0 until (a?.length() ?: 0)).map { a!!.getString(it) }

/** Linking a device the first time (QR or network discovery). */
class Pairing(val base: String, val trust: PinTrust) {
    private val http = pinnedClient(trust)
    private val nonce = randomBytes(16)
    var pairId = ""; var macNonce = ByteArray(0)

    fun start(method: String, token: String, deviceName: String): String {
        val n = b64(nonce)
        val j = http.postJson("$base/v1/pair/start", JSONObject()
            .put("method", method).put("token", token).put("device_name", deviceName)
            .put("pubkey", Keys.publicKey()).put("nonce", n)
            .put("sig", Keys.sign("echolink-pair-v1|$method|$n|$deviceName")))
        pairId = j.getString("pair_id"); macNonce = unb64(j.getString("mac_nonce"))
        return pairCode(trust.seen!!, nonce, macNonce)
    }

    fun status(): JSONObject = http.getJson("$base/v1/pair/$pairId")
}

enum class Conn { CONNECTING, CONNECTED, RECONNECTING, OFFLINE, REMOVED }

/** Keeps the conversation with Echo open: tries every way to reach the Mac at once (home addresses, the network
 *  discovery result, Tailscale name, Tailscale address), uses the first that answers, signs in with this device's
 *  key, and starts over at once when the phone's network changes. Retries forever (1, 2, 4 … 15 s). */
class EchoLink(private val ctx: Context, var linked: Linked) {
    val state = MutableStateFlow(Conn.CONNECTING)
    val route = MutableStateFlow("")               // "home Wi-Fi" or "Tailscale", shown on the status screen
    val events = MutableSharedFlow<JSONObject>(extraBufferCapacity = 256)
    val lastConnected = MutableStateFlow(0L)
    @Volatile var discovered: String? = null        // host from network discovery, if any
    private val trust = PinTrust(unb64(linked.pin))
    val http = pinnedClient(trust, readSeconds = 60)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val outbox = Channel<String>(Channel.UNLIMITED)   // said while away: sent after reconnecting
    @Volatile private var ws: WebSocket? = null
    @Volatile var base: String = ""; @Volatile var session: String = ""
    private val kick = Channel<Unit>(Channel.CONFLATED)
    private var loop: Job? = null

    private val netCb = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) { kick.trySend(Unit) }
        override fun onLost(network: Network) { kick.trySend(Unit) }
        override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) { }
    }

    fun start() {
        val cm = ctx.getSystemService(ConnectivityManager::class.java)
        try { cm.registerNetworkCallback(NetworkRequest.Builder().build(), netCb) } catch (_: Exception) {}
        loop = scope.launch { run() }
        scope.launch { for (m in outbox) { while (!(ws?.send(m) ?: false)) { delay(500) } } }
    }

    fun stop() {
        try { ctx.getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(netCb) } catch (_: Exception) {}
        ws?.close(1000, "bye"); loop?.cancel()
    }

    fun reconnectNow() { kick.trySend(Unit) }

    fun say(text: String, voice: Boolean) = sayJson(JSONObject().put("text", text).put("how", if (voice) "voice" else "typed"))

    /** Queued: if the link is down it is sent as soon as it is back. */
    fun sayJson(m: JSONObject) { outbox.trySend(m.toString()) }

    private fun routes(): List<Pair<String, String>> {
        val l = linked
        val r = mutableListOf<Pair<String, String>>()
        discovered?.let { r += "https://$it:${l.port}" to "home Wi-Fi" }
        l.lan.forEach { r += "https://$it:${l.port}" to "home Wi-Fi" }
        if (l.ts.isNotBlank()) r += "https://${l.ts}:${l.port}" to "Tailscale"
        if (l.ti.isNotBlank()) r += "https://${l.ti}:${l.port}" to "Tailscale"
        return r.distinctBy { it.first }
    }

    /** Ask every route at once; the first to answer as THIS Mac wins (home Wi-Fi preferred if both answer fast). */
    private suspend fun race(): Pair<String, String>? = withContext(Dispatchers.IO) {
        val rs = routes()
        if (rs.isEmpty()) return@withContext null
        val quick = pinnedClient(trust).newBuilder().connectTimeout(3, TimeUnit.SECONDS)
            .readTimeout(3, TimeUnit.SECONDS).build()
        val tries = rs.map { r ->
            async {
                try { if (quick.getJson("${r.first}/v1/hello").optString("mac_id") == linked.macId) r else null }
                catch (e: Exception) { null }
            }
        }
        withTimeoutOrNull(6000) {
            var left = tries.toMutableList()
            var found: Pair<String, String>? = null
            while (left.isNotEmpty() && found == null) {
                val (d, v) = select { left.forEach { t -> t.onAwait { t to it } } }
                left.remove(d); if (v != null) found = v
            }
            if (found?.second == "Tailscale") {                    // give home Wi-Fi a moment: it's faster
                withTimeoutOrNull(400) {
                    while (left.isNotEmpty()) {
                        val (d, v) = select { left.forEach { t -> t.onAwait { t to it } } }
                        left.remove(d); if (v?.second == "home Wi-Fi") { found = v; break }
                    }
                }
            }
            tries.forEach { it.cancel() }
            found
        }
    }

    private fun signIn(b: String) {
        val c = http.postJson("$b/v1/auth/challenge", JSONObject().put("device_id", linked.deviceId)).getString("challenge")
        val s = http.postJson("$b/v1/auth/session", JSONObject().put("device_id", linked.deviceId)
            .put("challenge", c).put("sig", Keys.sign("echolink-auth-v1|${linked.deviceId}|$c")))
        session = s.getString("session")
    }

    private suspend fun run() {
        var wait = 1000L
        while (true) {
            state.value = if (lastConnected.value == 0L) Conn.CONNECTING else Conn.RECONNECTING
            val r = race()
            if (r != null) {
                try {
                    signIn(r.first)
                    base = r.first; route.value = r.second
                    val closed = Channel<Int>(1)
                    ws = http.newWebSocket(Request.Builder().url(r.first.replace("https://", "wss://") + "/v1/ws")
                        .header("Authorization", "Bearer $session").build(), object : WebSocketListener() {
                        override fun onOpen(webSocket: WebSocket, response: Response) {
                            state.value = Conn.CONNECTED; lastConnected.value = System.currentTimeMillis()
                        }
                        override fun onMessage(webSocket: WebSocket, text: String) {
                            try { events.tryEmit(JSONObject(text)) } catch (_: Exception) {}
                        }
                        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) { closed.trySend(code) }
                        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) { closed.trySend(-1) }
                    })
                    wait = 1000L
                    val code = select { closed.onReceive { it }; kick.onReceive { -2 } }
                    ws?.cancel(); ws = null
                    if (code == -2) continue                    // network changed: find the best route again now
                } catch (e: HttpError) {
                    if (e.code == 401) { state.value = Conn.REMOVED; return }   // removed on the Mac: link again
                } catch (_: Exception) { }
            }
            state.value = Conn.OFFLINE.takeIf { r == null && lastConnected.value == 0L } ?: Conn.RECONNECTING
            select<Unit> { kick.onReceive { }; scope.async { delay(wait) }.onAwait { } }
            wait = (wait * 2).coerceAtMost(15000L)
        }
    }

    /** Echo's voice for [text]: raw 16-bit mono PCM chunks. */
    fun tts(text: String): Response? = try {
        http.newCall(Request.Builder().url("$base/v1/tts").header("Authorization", "Bearer $session")
            .post(JSONObject().put("text", text).toString().toRequestBody(JSON)).build()).execute()
            .takeIf { it.isSuccessful }
    } catch (e: Exception) { null }

    /** Same as [tts] but not started yet, so the caller can cancel it (interrupting Echo also stops the Mac's voice). */
    fun ttsCall(text: String): okhttp3.Call = http.newCall(Request.Builder().url("$base/v1/tts")
        .header("Authorization", "Bearer $session").post(JSONObject().put("text", text).toString().toRequestBody(JSON)).build())

    fun stt(wav: ByteArray): String? = try {
        http.newCall(Request.Builder().url("$base/v1/stt").header("Authorization", "Bearer $session")
            .post(wav.toRequestBody("audio/wav".toMediaType())).build()).execute()
            .use { if (it.isSuccessful) JSONObject(it.body!!.string()).optString("text") else null }
    } catch (e: Exception) { null }

    fun sttFull(wav: ByteArray): Pair<String, String>? = try {
        http.newCall(Request.Builder().url("$base/v1/stt").header("Authorization", "Bearer $session")
            .post(wav.toRequestBody("audio/wav".toMediaType())).build()).execute()
            .use { if (it.isSuccessful) JSONObject(it.body!!.string()).let { j -> j.optString("text") to j.optString("speaker", "unknown") } else null }
    } catch (e: Exception) { null }

    fun face(expr: String): ByteArray? = try {
        http.newCall(Request.Builder().url("$base/v1/face/$expr").header("Authorization", "Bearer $session").build())
            .execute().use { if (it.isSuccessful) it.body!!.bytes() else null }
    } catch (e: Exception) { null }
}

/** Is everything set for reaching Echo away from home? */
object Away {
    const val TAILSCALE = "com.tailscale.ipn"
    fun installed(c: Context) = try { c.packageManager.getPackageInfo(TAILSCALE, 0); true } catch (e: Exception) { false }
    fun vpnOn(c: Context): Boolean {
        val cm = c.getSystemService(ConnectivityManager::class.java)
        return cm.allNetworks.any { cm.getNetworkCapabilities(it)?.hasTransport(NetworkCapabilities.TRANSPORT_VPN) == true }
    }
    fun deviceName(): String = (Build.MODEL ?: "device").let { m ->
        if (m.startsWith(Build.MANUFACTURER ?: "", ignoreCase = true)) m else "${Build.MANUFACTURER} $m"
    }.replace(Regex("[^\\w .'-]"), "").take(40)
}
