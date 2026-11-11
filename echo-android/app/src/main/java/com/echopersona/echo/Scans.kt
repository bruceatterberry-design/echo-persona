package com.echopersona.echo

import android.content.Context
import android.graphics.BitmapFactory
import android.os.Bundle
import android.os.CancellationSignal
import android.os.ParcelFileDescriptor
import android.print.PageRange
import android.print.PrintAttributes
import android.print.PrintDocumentAdapter
import android.print.PrintDocumentInfo
import android.print.PrintManager
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.RandomAccessFile
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

private val Cream = Color(0xFFEEE6D6)
private val Muted = Color(0xFFA89F8F)
private val CardBg = Color(0xFF1A1611)

/** A scan kept on this device until the Mac has it (then kept for printing / viewing away from home). */
data class LocalScan(val id: String, val name: String, val folder: String, val tags: List<String>, val size: Long,
                     val sha256: String, val created: Long, val docId: String?) {
    fun json() = JSONObject().put("id", id).put("name", name).put("folder", folder).put("tags", JSONArray(tags))
        .put("size", size).put("sha256", sha256).put("created", created).put("doc_id", docId ?: "")
}

class ScanStore(private val ctx: Context) {
    private val root = File(ctx.filesDir, "scans").apply { mkdirs() }
    fun dir(id: String) = File(root, id)
    fun pdf(id: String) = File(dir(id), "doc.pdf")
    fun thumb(id: String) = File(dir(id), "thumb.jpg")

    fun all(): List<LocalScan> = (root.listFiles() ?: emptyArray()).mapNotNull { d ->
        try { val j = JSONObject(File(d, "meta.json").readText())
            LocalScan(j.getString("id"), j.getString("name"), j.optString("folder"), jsonList(j.optJSONArray("tags")),
                j.getLong("size"), j.getString("sha256"), j.getLong("created"), j.optString("doc_id").ifBlank { null })
        } catch (e: Exception) { null }
    }.sortedByDescending { it.created }

    fun save(s: LocalScan) = File(dir(s.id), "meta.json").writeText(s.json().toString())

    fun add(pdfIn: java.io.InputStream, thumbIn: java.io.InputStream?, name: String, folder: String, tags: List<String>): LocalScan {
        val id = System.currentTimeMillis().toString(36) + randomBytes(3).joinToString("") { "%02x".format(it) }
        dir(id).mkdirs()
        pdfIn.use { i -> FileOutputStream(pdf(id)).use { i.copyTo(it) } }
        thumbIn?.use { i -> FileOutputStream(thumb(id)).use { i.copyTo(it) } }
        val md = MessageDigest.getInstance("SHA-256")
        FileInputStream(pdf(id)).use { i -> val b = ByteArray(1 shl 16); while (true) { val n = i.read(b); if (n <= 0) break; md.update(b, 0, n) } }
        val s = LocalScan(id, name, folder, tags, pdf(id).length(), md.digest().joinToString("") { "%02x".format(it) },
            System.currentTimeMillis(), null)
        save(s); return s
    }

    fun forgetDoc(docId: String) = all().filter { it.docId == docId }.forEach { dir(it.id).deleteRecursively() }
}

/** Sends waiting scans to the Mac in 1 MB pieces at the offset the Mac reports: an interrupted send continues where it
 *  stopped (even after the app restarts); the Mac checks the whole file's SHA-256. */
class Uploader(private val store: ScanStore, private val link: () -> EchoLink?) {
    val progress = MutableStateFlow<Map<String, String>>(emptyMap())     // local id -> "Sending 40%" / "Waiting…"
    private val kick = Channel<Unit>(Channel.CONFLATED)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    init { scope.launch { loop() } }
    fun kick() { kick.trySend(Unit) }

    private fun set(id: String, t: String?) { progress.value = progress.value.toMutableMap().apply { if (t == null) remove(id) else put(id, t) } }

    private suspend fun loop() {
        while (true) {
            for (s in store.all().filter { it.docId == null }) {
                val lk = link()
                if (lk == null || lk.state.value != Conn.CONNECTED) { set(s.id, "Saved here. It will be sent when Echo can be reached."); continue }
                try { send(lk, s) } catch (e: Exception) { set(s.id, "Couldn't send yet; trying again soon.") }
            }
            kotlinx.coroutines.withTimeoutOrNull(20_000) { kick.receive() }
        }
    }

    private fun send(lk: EchoLink, s: LocalScan) {
        val start = lk.http.postJson("${lk.base}/v1/uploads", JSONObject().put("kind", "scan").put("size", s.size)
            .put("sha256", s.sha256).put("name", s.name).put("folder", s.folder).put("tags", JSONArray(s.tags)), lk.session)
        val uid = start.getString("upload_id"); var off = start.getLong("offset")
        RandomAccessFile(store.pdf(s.id), "r").use { f ->
            val buf = ByteArray(1 shl 20)
            while (true) {
                set(s.id, "Sending ${if (s.size > 0) off * 100 / s.size else 0}%")
                f.seek(off); val n = f.read(buf); if (n <= 0) break
                val r = lk.http.newCall(Request.Builder().url("${lk.base}/v1/uploads/$uid").header("Authorization", "Bearer ${lk.session}")
                    .header("Upload-Offset", off.toString()).put(buf.copyOf(n).toRequestBody("application/octet-stream".toMediaType())).build())
                    .execute().use { r -> if (!r.isSuccessful) throw HttpError(r.code, r.body?.string() ?: ""); JSONObject(r.body!!.string()) }
                if (r.optBoolean("done")) { store.save(s.copy(docId = r.getString("doc_id"))); set(s.id, null); return }
                off = r.optLong("resend_from", r.getLong("offset"))
            }
        }
    }
}

class PdfPrint(private val file: File, private val name: String) : PrintDocumentAdapter() {
    override fun onLayout(old: PrintAttributes?, new: PrintAttributes?, cancel: CancellationSignal?, cb: LayoutResultCallback, extras: Bundle?) {
        if (cancel?.isCanceled == true) { cb.onLayoutCancelled(); return }
        cb.onLayoutFinished(PrintDocumentInfo.Builder("$name.pdf").setContentType(PrintDocumentInfo.CONTENT_TYPE_DOCUMENT).build(), true)
    }
    override fun onWrite(pages: Array<out PageRange>?, dest: ParcelFileDescriptor, cancel: CancellationSignal?, cb: WriteResultCallback) {
        try {
            FileInputStream(file).use { i -> FileOutputStream(dest.fileDescriptor).use { i.copyTo(it) } }
            cb.onWriteFinished(arrayOf(PageRange.ALL_PAGES))
        } catch (e: Exception) { cb.onWriteFailed(e.message) }
    }
}

/** One row in the list: the Mac's copy (searchable, read) and/or the copy on this device. */
data class DocRow(val docId: String?, val localId: String?, val name: String, val folder: String, val tags: List<String>,
                  val pages: Int, val status: String, val snippet: String)

/** SCANNER (the owner): scan → name it → it is kept here and sent to Echo; search, open, rename, print, ask Echo. */
@Composable
fun ScannerScreen(store: ScanStore, uploader: Uploader, link: EchoLink?, conn: Conn, scan: () -> Unit,
                  newScan: Triple<java.io.InputStream, java.io.InputStream?, Int>?, clearNewScan: () -> Unit,
                  ask: (String, String) -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var q by remember { mutableStateOf("") }
    var mac by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    var local by remember { mutableStateOf(store.all()) }
    var open by remember { mutableStateOf<DocRow?>(null) }
    var tick by remember { mutableStateOf(0) }
    val prog by uploader.progress.collectAsState()

    LaunchedEffect(q, conn, tick, prog) {
        delay(300)
        local = store.all()
        if (conn == Conn.CONNECTED && link != null) withContext(Dispatchers.IO) {
            try { mac = jsonObjects(link.http.getJson("${link.base}/v1/docs?q=${java.net.URLEncoder.encode(q, "UTF-8")}", link.session).optJSONArray("docs")) }
            catch (_: Exception) { }
        }
    }
    LaunchedEffect(mac) { if (mac.any { it.optString("status") == "reading" }) { delay(3000); tick++ } }

    if (newScan != null) {
        SaveScan(newScan.third, onCancel = clearNewScan) { name, folder, tags ->
            store.add(newScan.first, newScan.second, name, folder, tags)
            clearNewScan(); uploader.kick(); local = store.all()
        }
        return
    }
    open?.let { row -> DocDetail(row, store, link, conn, { open = null; tick++ }, ask); return }

    val sentIds = local.mapNotNull { it.docId }.toSet()
    val rows = local.filter { it.docId == null && (q.isBlank() || (it.name + " " + it.folder + " " + it.tags.joinToString(" ")).contains(q, true)) }
        .map { DocRow(null, it.id, it.name, it.folder, it.tags, 0, prog[it.id] ?: "Waiting to send", "") } +
        mac.map { j -> val id = j.getString("id")
            DocRow(id, local.firstOrNull { it.docId == id }?.id, j.getString("name"), j.optString("folder"), jsonList(j.optJSONArray("tags")),
                j.optInt("pages"), when (j.optString("status")) { "reading" -> "Echo is reading it…"; "failed" -> "Couldn't read the text"; else -> "Ready" },
                j.optString("snippet")) } +
        (if (conn != Conn.CONNECTED) local.filter { it.docId != null && it.docId in sentIds }.map {
            DocRow(it.docId, it.id, it.name, it.folder, it.tags, 0, "On Echo (away right now)", "") } else emptyList())

    Column(Modifier.fillMaxSize().padding(horizontal = 18.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Button(scan, Modifier.height(56.dp), shape = RoundedCornerShape(14.dp),
                colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Ink)) { Text("Scan a document", fontSize = 18.sp) }
            Spacer(Modifier.size(14.dp))
            OutlinedTextField(q, { q = it }, Modifier.weight(1f), singleLine = true, placeholder = { Text("Search words in your documents") },
                colors = fieldColors())
        }
        if (conn != Conn.CONNECTED) Text("Echo can't be reached right now: new scans are kept here and sent later. Search finds names only.",
            color = Color(0xFFE0B060), fontSize = 15.sp, modifier = Modifier.padding(top = 8.dp))
        LazyColumn(Modifier.fillMaxSize().padding(top = 12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            if (rows.isEmpty()) item { Text(if (q.isBlank()) "No documents yet. Tap “Scan a document”." else "Nothing found for “$q”.",
                color = Muted, fontSize = 17.sp, modifier = Modifier.padding(20.dp)) }
            items(rows, key = { (it.docId ?: "") + "/" + (it.localId ?: "") }) { r ->
                Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(CardBg).border(1.dp, Color(0xFF3A3020), RoundedCornerShape(14.dp))
                    .clickable { open = r }.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Thumb(store, link, r, 64)
                    Column(Modifier.weight(1f).padding(start = 14.dp)) {
                        Text(r.name, color = Cream, fontSize = 18.sp)
                        Text(listOfNotNull(r.folder.ifBlank { null }, r.tags.takeIf { it.isNotEmpty() }?.joinToString(", "),
                            if (r.pages > 0) "${r.pages} page${if (r.pages == 1) "" else "s"}" else null).joinToString(" · "), color = Muted, fontSize = 14.sp)
                        if (r.snippet.isNotBlank()) Text("…${r.snippet}…", color = Color(0xFFD9CDB5), fontSize = 14.sp)
                        Text(r.status, color = if (r.status == "Ready") Color(0xFF8BC48A) else Color(0xFFE0B060), fontSize = 14.sp)
                    }
                }
            }
        }
    }
}

@Composable
private fun Thumb(store: ScanStore, link: EchoLink?, r: DocRow, size: Int) {
    val ctx = LocalContext.current
    var bmp by remember(r) { mutableStateOf(r.localId?.let { store.thumb(it) }?.takeIf { it.exists() }?.let { BitmapFactory.decodeFile(it.path) }) }
    LaunchedEffect(r) {
        if (bmp == null && r.docId != null && link != null) withContext(Dispatchers.IO) {
            val f = File(ctx.cacheDir, "thumb-${r.docId}.jpg")
            if (!f.exists()) try { link.http.newCall(Request.Builder().url("${link.base}/v1/docs/${r.docId}/thumb")
                .header("Authorization", "Bearer ${link.session}").build()).execute().use { if (it.isSuccessful) f.writeBytes(it.body!!.bytes()) } } catch (_: Exception) {}
            if (f.exists()) bmp = BitmapFactory.decodeFile(f.path)
        }
    }
    Box(Modifier.size(size.dp, (size * 1.3).dp).clip(RoundedCornerShape(6.dp)).background(Color(0xFF2A241C))) {
        bmp?.let { Image(it.asImageBitmap(), null, Modifier.fillMaxSize(), contentScale = ContentScale.Crop) }
    }
}

@Composable
private fun SaveScan(pages: Int, onCancel: () -> Unit, onSave: (String, String, List<String>) -> Unit) {
    val ctx = LocalContext.current
    val prefs = ctx.getSharedPreferences("scanner", Context.MODE_PRIVATE)
    var name by remember { mutableStateOf("Scan " + SimpleDateFormat("MMM d, h:mm a", Locale.US).format(Date())) }
    var folder by remember { mutableStateOf(prefs.getString("folder", "Documents")!!) }
    var tags by remember { mutableStateOf("") }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Text("Scanned $pages page${if (pages == 1) "" else "s"}", color = Cream, fontSize = 24.sp)
        Spacer(Modifier.height(16.dp))
        Column(Modifier.widthIn(max = 560.dp).fillMaxWidth()) {
            OutlinedTextField(name, { name = it }, Modifier.fillMaxWidth(), label = { Text("Name") }, singleLine = true, colors = fieldColors())
            OutlinedTextField(folder, { folder = it }, Modifier.fillMaxWidth().padding(top = 8.dp), label = { Text("Folder") }, singleLine = true, colors = fieldColors())
            OutlinedTextField(tags, { tags = it }, Modifier.fillMaxWidth().padding(top = 8.dp), label = { Text("Tags (comma between)") }, singleLine = true, colors = fieldColors())
        }
        Row(Modifier.padding(top = 18.dp)) {
            TextButton(onCancel) { Text("Throw away", color = Muted, fontSize = 17.sp) }
            Spacer(Modifier.size(12.dp))
            Button({ prefs.edit().putString("folder", folder.trim().ifBlank { "Documents" }).apply()
                onSave(name.trim().ifBlank { "Scan" }, folder.trim(), tags.split(",").map { it.trim() }.filter { it.isNotBlank() }) },
                Modifier.height(54.dp), shape = RoundedCornerShape(14.dp), colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Ink)) {
                Text("Save and send to Echo", fontSize = 17.sp)
            }
        }
    }
}

@Composable
private fun DocDetail(r: DocRow, store: ScanStore, link: EchoLink?, conn: Conn, back: () -> Unit, ask: (String, String) -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var name by remember { mutableStateOf(r.name) }
    var folder by remember { mutableStateOf(r.folder) }
    var tags by remember { mutableStateOf(r.tags.joinToString(", ")) }
    var text by remember { mutableStateOf("") }
    var note by remember { mutableStateOf("") }
    var sure by remember { mutableStateOf(false) }
    LaunchedEffect(r.docId, conn) {
        if (r.docId != null && link != null && conn == Conn.CONNECTED) withContext(Dispatchers.IO) {
            try { val j = link.http.getJson("${link.base}/v1/docs/${r.docId}", link.session)
                text = jsonObjects(j.optJSONArray("text")).joinToString("\n\n") { "— page ${it.getInt("page")} —\n${it.getString("text")}" }
            } catch (_: Exception) { }
        }
    }
    fun printIt() = scope.launch {
        val f = r.localId?.let { store.pdf(it) }?.takeIf { it.exists() } ?: withContext(Dispatchers.IO) {
            if (r.docId == null || link == null) null else try {
                File(ctx.cacheDir, "print-${r.docId}.pdf").also { out -> link.http.newCall(Request.Builder().url("${link.base}/v1/docs/${r.docId}/pdf")
                    .header("Authorization", "Bearer ${link.session}").build()).execute().use { out.writeBytes(it.body!!.bytes()) } }
            } catch (e: Exception) { null }
        }
        if (f == null) { note = "The PDF isn't on this device and Echo can't be reached right now."; return@launch }
        ctx.getSystemService(PrintManager::class.java).print(name, PdfPrint(f, name), PrintAttributes.Builder().build())
    }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp)) {
        TextButton(back) { Text("‹ All documents", color = Gold, fontSize = 17.sp) }
        Row {
            Thumb(store, link, r, 120)
            Column(Modifier.weight(1f).padding(start = 18.dp)) {
                OutlinedTextField(name, { name = it }, Modifier.fillMaxWidth(), label = { Text("Name") }, singleLine = true, colors = fieldColors())
                OutlinedTextField(folder, { folder = it }, Modifier.fillMaxWidth().padding(top = 6.dp), label = { Text("Folder") }, singleLine = true, colors = fieldColors())
                OutlinedTextField(tags, { tags = it }, Modifier.fillMaxWidth().padding(top = 6.dp), label = { Text("Tags") }, singleLine = true, colors = fieldColors())
                if (r.docId != null) TextButton({ scope.launch { withContext(Dispatchers.IO) { try {
                    link?.http?.newCall(Request.Builder().url("${link.base}/v1/docs/${r.docId}").header("Authorization", "Bearer ${link.session}")
                        .patch(JSONObject().put("name", name).put("folder", folder).put("tags", JSONArray(tags.split(",").map { it.trim() }.filter { it.isNotBlank() }))
                            .toString().toRequestBody("application/json".toMediaType())).build())?.execute()?.close()
                    note = "Saved." } catch (e: Exception) { note = "Couldn't save the changes right now." } } } }) { Text("Save changes", color = Gold) }
            }
        }
        Row(Modifier.padding(vertical = 12.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            if (r.docId != null) Button({ ask(r.docId, name) }, Modifier.height(54.dp), shape = RoundedCornerShape(14.dp),
                colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Ink)) { Text("Ask Echo about this", fontSize = 17.sp) }
            Button({ printIt() }, Modifier.height(54.dp), shape = RoundedCornerShape(14.dp),
                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF2A241C), contentColor = Cream)) { Text("Print or save PDF", fontSize = 17.sp) }
        }
        if (note.isNotBlank()) Text(note, color = Color(0xFFE0B060), fontSize = 15.sp)
        if (r.docId == null) Text("Waiting to be sent to Echo. Once he has it you can search its words and ask him about it.", color = Muted, fontSize = 15.sp)
        if (text.isNotBlank()) {
            Text("What Echo read", color = Gold, fontSize = 18.sp, modifier = Modifier.padding(top = 12.dp))
            Text(text, color = Cream, fontSize = 16.sp, lineHeight = 23.sp, modifier = Modifier.padding(top = 6.dp))
        }
        Spacer(Modifier.height(20.dp))
        if (!sure) TextButton({ sure = true }) { Text("Delete this document", color = Color(0xFFE08A70)) }
        else TextButton({ scope.launch {
            val ok = withContext(Dispatchers.IO) { try {
                if (r.docId != null) link?.http?.newCall(Request.Builder().url("${link.base}/v1/docs/${r.docId}")
                    .header("Authorization", "Bearer ${link.session}").delete().build())?.execute()?.use { it.isSuccessful || it.code == 404 } ?: false else true
            } catch (e: Exception) { false } }
            if (ok) { r.docId?.let { store.forgetDoc(it) }; r.localId?.let { store.dir(it).deleteRecursively() }; back() }
            else note = "Couldn't delete it on the Mac right now; try again when Echo can be reached."
        } }) { Text("Yes, delete it here and on the Mac", color = Color(0xFFE08A70)) }
    }
}

@Composable
private fun fieldColors() = OutlinedTextFieldDefaults.colors(focusedTextColor = Cream, unfocusedTextColor = Cream,
    focusedBorderColor = Gold, unfocusedBorderColor = Color(0xFF3A3020), focusedLabelColor = Gold, unfocusedLabelColor = Muted)

fun jsonObjects(a: JSONArray?): List<JSONObject> = (0 until (a?.length() ?: 0)).map { a!!.getJSONObject(it) }
