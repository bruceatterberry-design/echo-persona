package com.echopersona.collector

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * On-phone dataset. Everything lives in the app's own storage:
 *   files/consent.json
 *   files/sessions/<id>/take-NNN.(wav|mp4) + take-NNN.json   one recording + its metadata
 *   files/sessions/<id>/manifest.json                        written when the session is finished
 *   files/receipts/<id>.json                                 written by the Mac (over USB) after it verified every file
 * Nothing is deleted unless a verified receipt exists AND the user turned on "free space after copy".
 */
class Store(context: Context) {
    val root: File = context.getExternalFilesDir(null) ?: context.filesDir
    private val sessions = File(root, "sessions").apply { mkdirs() }
    private val receipts = File(root, "receipts").apply { mkdirs() }
    private val prefs = context.getSharedPreferences("collector", Context.MODE_PRIVATE)

    // ---- consent ----
    fun consent(): JSONObject? = File(root, "consent.json").takeIf { it.exists() }?.let { JSONObject(it.readText()) }

    fun giveConsent(name: String) {
        File(root, "consent.json").writeText(JSONObject().apply {
            put("name", name.trim())
            put("statement", CONSENT_TEXT)
            put("granted_at", now())
            put("scope", JSONArray(listOf("voice_cloning", "face_animation", "personality_modeling")))
            put("transfer", "usb_only")
        }.toString(2))
    }

    // ---- settings ----
    var freeSpaceAfterCopy: Boolean
        get() = prefs.getBoolean("free_space", false)
        set(v) = prefs.edit().putBoolean("free_space", v).apply()
    var paused: Boolean
        get() = prefs.getBoolean("paused", false)
        set(v) = prefs.edit().putBoolean("paused", v).apply()
    var targetDays: Int
        get() = prefs.getInt("target_days", 14)
        set(v) = prefs.edit().putInt("target_days", v).apply()

    // ---- sessions and takes ----
    fun newSession(): File {
        var id = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
        while (File(sessions, id).exists()) id += "x"
        return File(sessions, id).apply { mkdirs() }
    }

    fun nextTakeFile(session: File, ext: String): File {
        val n = (session.listFiles { f -> f.name.startsWith("take-") && !f.name.endsWith(".json") }?.size ?: 0) + 1
        return File(session, "take-%03d.%s".format(n, ext))
    }

    /** Write a take's metadata (once the recording file is closed). */
    fun saveTakeMeta(media: File, task: Task, durationMs: Long, extra: JSONObject, kept: Boolean, status: String? = null) {
        val meta = JSONObject().apply {
            put("session", media.parentFile!!.name)
            put("take", media.nameWithoutExtension)
            put("file", media.name)
            put("task_id", task.id)
            put("task_kind", task.kind.name.lowercase())
            put("category", task.category)
            put("prompt_text", task.text)
            put("expression_label", task.expressionLabel ?: JSONObject.NULL)  // the instructed task, not a diagnosis
            put("life_period", task.lifePeriod ?: JSONObject.NULL)            // memories: childhood, teen, …
            put("recorded_at", now())
            put("duration_ms", durationMs)
            put("bytes", media.length())
            put("sha256", sha256(media))
            put("status", status ?: if (kept) "kept" else "rejected")
            put("consent", "consent.json")
            extra.keys().forEach { put(it, extra.get(it)) }
        }
        File(media.parentFile, media.nameWithoutExtension + ".json").writeText(meta.toString(2))
    }

    fun finishSession(session: File, completed: Boolean) {
        val takes = JSONArray()
        session.listFiles { f -> f.name.startsWith("take-") && f.name.endsWith(".json") }
            ?.sortedBy { it.name }?.forEach { takes.put(JSONObject(it.readText())) }
        if (takes.length() == 0) { session.deleteRecursively(); return }
        File(session, "manifest.json").writeText(JSONObject().apply {
            put("session", session.name)
            put("finished_at", now())
            put("completed", completed)
            put("app_version", BuildConfigInfo.VERSION)
            put("takes", takes)
        }.toString(2))
    }

    /**
     * Close sessions that were left open (app closed or switched away mid-session). Recordings without a Keep/Redo
     * decision are kept and marked "unreviewed" (never deleted), then the session gets its manifest so it can be copied.
     */
    fun recoverOpenSessions(): Int {
        var n = 0
        sessions.listFiles()?.filter { it.isDirectory && !File(it, "manifest.json").exists() }?.forEach { d ->
            d.listFiles { f -> f.extension in setOf("wav", "mp4", "jpg") }?.forEach { media ->
                if (File(d, media.nameWithoutExtension + ".json").exists() || media.length() == 0L) return@forEach
                val kind = when (media.extension) { "wav" -> Kind.VOICE; "jpg" -> Kind.PHOTO; else -> Kind.FACE }
                val dur = if (kind == Kind.PHOTO) 0L else if (kind == Kind.VOICE) (media.length() - 44) * 1000 / (48000 * 2) else try {
                    android.media.MediaMetadataRetriever().use { m ->
                        m.setDataSource(media.absolutePath)
                        m.extractMetadata(android.media.MediaMetadataRetriever.METADATA_KEY_DURATION)?.toLongOrNull() ?: 0L
                    }
                } catch (_: Exception) { 0L }
                saveTakeMeta(media, Task("unknown", kind, "unknown", "", "", 0), dur,
                    JSONObject().put("note", "recovered: session was left before this take was reviewed"), true, "unreviewed")
            }
            finishSession(d, completed = false)
            n++
        }
        return n
    }

    data class Summary(val sessions: Int, val completed: Int, val keptMinutes: Double, val bytes: Long,
                       val waiting: Int, val transferred: Int, val days: Int, val todayCount: Int)

    fun summary(): Summary {
        val dirs = sessions.listFiles()?.filter { File(it, "manifest.json").exists() } ?: emptyList()
        var completed = 0; var keptMs = 0L; var bytes = 0L; var transferred = 0
        val days = HashSet<String>(); var today = 0
        val todayKey = SimpleDateFormat("yyyyMMdd", Locale.US).format(Date())
        for (d in dirs) {
            val m = JSONObject(File(d, "manifest.json").readText())
            if (m.optBoolean("completed")) completed++
            days.add(d.name.take(8)); if (d.name.startsWith(todayKey)) today++
            val t = m.getJSONArray("takes")
            for (i in 0 until t.length()) t.getJSONObject(i).let {
                if (it.optString("status") == "kept") keptMs += it.optLong("duration_ms")
                bytes += it.optLong("bytes")
            }
            if (File(receipts, d.name + ".json").exists()) transferred++
        }
        return Summary(dirs.size, completed, keptMs / 60000.0, bytes, dirs.size - transferred, transferred, days.size, today)
    }

    /** After the Mac's verified receipt: optionally remove media to free space (metadata and manifest are kept). */
    fun freeVerifiedSpace(): Int {
        if (!freeSpaceAfterCopy) return 0
        var n = 0
        sessions.listFiles()?.forEach { d ->
            val r = File(receipts, d.name + ".json")
            if (!r.exists()) return@forEach
            val ok = JSONObject(r.readText()).optJSONObject("verified") ?: return@forEach
            d.listFiles { f -> f.extension in setOf("wav", "mp4", "jpg") }?.forEach { f ->
                if (ok.optString(f.name) == sha256(f) && f.delete()) n++   // only files the Mac verified byte-for-byte
            }
        }
        return n
    }

    companion object {
        const val CONSENT_TEXT = "I, the person named below, authorize these recordings of my voice, face and words to be " +
            "used to create Echo, my digital self, on my own computer. They are copied only over a USB cable to " +
            "my Mac and are not uploaded to any outside service without my separate permission."

        fun now(): String = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssZ", Locale.US).format(Date())

        fun sha256(f: File): String {
            val md = MessageDigest.getInstance("SHA-256")
            f.inputStream().use { s -> val b = ByteArray(1 shl 16); while (true) { val n = s.read(b); if (n < 0) break; md.update(b, 0, n) } }
            return md.digest().joinToString("") { "%02x".format(it) }
        }
    }
}

object BuildConfigInfo { const val VERSION = "0.1.0" }
