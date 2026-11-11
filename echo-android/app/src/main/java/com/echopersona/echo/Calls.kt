package com.echopersona.echo

import android.content.ContentUris
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.ContactsContract
import android.provider.ContactsContract.CommonDataKinds
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
import androidx.compose.foundation.shape.CircleShape
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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject

private val Cream = Color(0xFFEEE6D6)
private val Muted = Color(0xFFA89F8F)
private val CardBg = Color(0xFF1A1611)
private val Warn = Color(0xFFE0B060)

data class Phone(val number: String, val label: String)
data class AppAction(val label: String, val intent: Intent)
data class Contact(val id: Long, val name: String, val starred: Boolean, val phones: List<Phone>, val apps: List<AppAction>,
                   val emails: List<String> = emptyList())

/** What the user asked for out loud ("call the loved one", "text the loved one I'm on my way"), to confirm before anything happens. */
data class PhoneAction(val kind: String, val matches: List<Contact>, val body: String = "", val heard: String = "")

/** Calling and texting through the apps on this device. Android lets no app listen to or record calls in other apps,
 *  so Echo helps before and after; the person always taps to call or send. Contacts stay on the device (only a name
 *  goes to the Mac, and only when Echo is asked to help). */
object Phonebook {
    private val STANDARD = setOf(CommonDataKinds.Phone.CONTENT_ITEM_TYPE, CommonDataKinds.Email.CONTENT_ITEM_TYPE,
        CommonDataKinds.StructuredName.CONTENT_ITEM_TYPE, CommonDataKinds.Photo.CONTENT_ITEM_TYPE,
        CommonDataKinds.GroupMembership.CONTENT_ITEM_TYPE, CommonDataKinds.Note.CONTENT_ITEM_TYPE,
        CommonDataKinds.Organization.CONTENT_ITEM_TYPE, CommonDataKinds.StructuredPostal.CONTENT_ITEM_TYPE,
        CommonDataKinds.Website.CONTENT_ITEM_TYPE, CommonDataKinds.Nickname.CONTENT_ITEM_TYPE, CommonDataKinds.Im.CONTENT_ITEM_TYPE,
        CommonDataKinds.SipAddress.CONTENT_ITEM_TYPE, CommonDataKinds.Identity.CONTENT_ITEM_TYPE, CommonDataKinds.Event.CONTENT_ITEM_TYPE,
        CommonDataKinds.Relation.CONTENT_ITEM_TYPE)
    @Volatile var cache: List<Contact> = emptyList()
    @Volatile private var local: List<Contact> = emptyList()
    @Volatile private var mac: List<Contact> = emptyList()

    /** Contacts from the Mac (its Contacts app, synced with the iPhone through iCloud), merged with this tablet's:
     *  same name or same number → one person with every number. */
    fun setMac(arr: JSONArray?) {
        mac = jsonObjects(arr).mapIndexed { i, o ->
            Contact(-1L - i, o.optString("name"), false, jsonObjects(o.optJSONArray("phones")).map { Phone(it.optString("number"), it.optString("label").ifBlank { "phone" }) },
                emptyList())
        }.filter { it.name.isNotBlank() && it.phones.isNotEmpty() }
        merge()
    }

    private fun digits(n: String) = n.filter(Char::isDigit).let { if (it.length == 11 && it.startsWith("1")) it.drop(1) else it }

    private fun merge() {
        val out = local.toMutableList()
        for (m in mac) {
            val i = out.indexOfFirst { c -> c.name.equals(m.name, true) || c.phones.any { p -> m.phones.any { digits(it.number) == digits(p.number) } } }
            if (i < 0) out += m
            else out[i] = out[i].copy(
                name = if (m.name.length > out[i].name.length && m.name.lowercase().startsWith(out[i].name.lowercase().split(" ").first())) m.name else out[i].name,
                phones = out[i].phones + m.phones.filter { mp -> out[i].phones.none { digits(it.number) == digits(mp.number) } })
        }
        cache = out.sortedBy { it.name.lowercase() }
    }

    fun load(c: Context): List<Contact> {
        val names = mutableMapOf<Long, Pair<String, Boolean>>()
        c.contentResolver.query(ContactsContract.Contacts.CONTENT_URI, arrayOf(ContactsContract.Contacts._ID,
            ContactsContract.Contacts.DISPLAY_NAME_PRIMARY, ContactsContract.Contacts.STARRED), null, null, null)?.use { q ->
            while (q.moveToNext()) q.getString(1)?.let { names[q.getLong(0)] = it to (q.getInt(2) == 1) }
        }
        val phones = mutableMapOf<Long, MutableList<Phone>>()
        val apps = mutableMapOf<Long, MutableList<AppAction>>()
        val emails = mutableMapOf<Long, MutableList<String>>()
        c.contentResolver.query(ContactsContract.Data.CONTENT_URI, arrayOf(ContactsContract.Data._ID, ContactsContract.Data.CONTACT_ID,
            ContactsContract.Data.MIMETYPE, ContactsContract.Data.DATA1, ContactsContract.Data.DATA2, ContactsContract.Data.DATA3),
            null, null, null)?.use { q ->
            while (q.moveToNext()) {
                val cid = q.getLong(1); val mime = q.getString(2) ?: continue
                if (mime == CommonDataKinds.Phone.CONTENT_ITEM_TYPE) {
                    val num = q.getString(3) ?: continue
                    val label = CommonDataKinds.Phone.getTypeLabel(c.resources, q.getInt(4), q.getString(5) ?: "").toString()
                    phones.getOrPut(cid) { mutableListOf() }.takeIf { l -> l.none { it.number.filter(Char::isDigit) == num.filter(Char::isDigit) } }?.add(Phone(num, label))
                } else if (mime == CommonDataKinds.Email.CONTENT_ITEM_TYPE) {
                    q.getString(3)?.let { emails.getOrPut(cid) { mutableListOf() }.add(it) }
                } else if (mime !in STANDARD) {
                    // other apps' contact actions (Google Meet call, WhatsApp message/call, Signal …), the same rows
                    // the Contacts app shows; opening one hands it to that app
                    val i = Intent(Intent.ACTION_VIEW).setDataAndType(ContentUris.withAppendedId(ContactsContract.Data.CONTENT_URI, q.getLong(0)), mime)
                    val ri = c.packageManager.resolveActivity(i, 0) ?: continue
                    val app = ri.loadLabel(c.packageManager).toString()
                    val label = (q.getString(5) ?: q.getString(4) ?: "").ifBlank { app }
                    apps.getOrPut(cid) { mutableListOf() }.add(AppAction(if (label.contains(app, true)) label else "$app: $label", i))
                }
            }
        }
        local = names.map { (id, n) -> Contact(id, n.first, n.second, phones[id] ?: emptyList(), apps[id] ?: emptyList(), emails[id] ?: emptyList()) }
            .filter { it.phones.isNotEmpty() || it.apps.isNotEmpty() || it.emails.isNotEmpty() }
        merge()
        return cache
    }

    /** Who is meant: exact full or first name first; else a name that SOUNDS the same (speech-to-text writes
     *  "Sammie" for the loved one, "Dannie" for Danny). Several matches → the card asks which one. */
    fun find(name: String, contacts: List<Contact> = cache): List<Contact> {
        val n = name.trim().lowercase().trimEnd(',', ':', '.', '\'')
        if (n.isBlank()) return emptyList()
        val first = n.split(" ").first()
        contacts.filter { it.name.lowercase() == n }.let { if (it.isNotEmpty()) return it }
        contacts.filter { it.name.lowercase().split(" ").first() == n || it.name.lowercase().startsWith("$n ") }.let { if (it.isNotEmpty()) return it }
        val words = n.split(" ")
        val strict = contacts.filter { c ->
            val cw = c.name.lowercase().split(" ")
            when {
                words.size >= 2 && cw.size >= 2 -> sounds(words[0], cw[0]) && sounds(words.last(), cw.last())
                words.size == 1 -> sounds(first, cw[0])
                else -> false               // "loved_one and introduce" is not a name, even if a contact is just "the loved one"
            }
        }
        return strict.ifEmpty { bestMatch(n, contacts) }
    }

    /** The go-between: what Echo heard, scored against every contact (whole name, first, last, sound-alikes), so a
     *  misheard or misspelled name ("Sammie Smyth") still finds the right person. A clear winner, or the few that
     *  are close (the card then asks which one). */
    fun bestMatch(heard: String, contacts: List<Contact> = cache): List<Contact> {
        val h = heard.lowercase().replace(Regex("[^a-z' -]"), " ").split(Regex("\\s+")).filter { it.isNotBlank() }
        if (h.isEmpty() || h.size > 4) return emptyList()
        fun sim(a: String, b: String) = if (a.isEmpty() || b.isEmpty()) 0.0 else 1.0 - lev(a, b).toDouble() / maxOf(a.length, b.length)
        fun tok(a: String, b: String) = maxOf(sim(a, b), if (a.length > 1 && b.length > 1 && a[0] == b[0] && soundex(a) == soundex(b)) 0.8 else 0.0)
        val scored = contacts.map { c ->
            val cw = c.name.lowercase().replace(Regex("\\(.*?\\)"), " ").split(Regex("\\s+"))
                .filter { it.isNotBlank() && it !in DESCRIBERS }
            val s = when {
                cw.isEmpty() -> 0.0
                h.size == 1 -> maxOf(tok(h[0], cw[0]), 0.9 * cw.maxOf { tok(h[0], it) })
                h.size == 2 && cw.size >= 2 -> (tok(h[0], cw[0]) + tok(h[1], cw.last())) / 2
                h.size == 2 && cw.size == 1 && h[1] !in NOT_NAMES && h[1].length >= 3 -> tok(h[0], cw[0]) * 0.85
                else -> sim(h.joinToString(" "), cw.joinToString(" "))
            }
            c to s
        }.filter { it.second >= 0.72 }.sortedByDescending { it.second }
        if (scored.isEmpty()) return emptyList()
        val top = scored[0].second
        return scored.filter { it.second >= top - 0.08 }.map { it.first }.take(4)
    }

    /** "call Walmart pharmacy", "phone the county clerk and ask …": someone to call, whoever they are. */
    fun callTarget(said: String): Pair<String, String>? {
        val t = said.trim().trimEnd('.', '!', '?').replace(Regex("^(?:hey |ok |okay )?echo[,]?\\s+", RegexOption.IGNORE_CASE), "")
            .replace(Regex("^(?:please |can you |could you |would you |will you )+", RegexOption.IGNORE_CASE), "")
        val m = Regex("^(?:call|phone|ring|dial)\\s+(?:up\\s+)?(?:the\\s+)?(.+?)(?:\\s+and\\s+(.+))?$", RegexOption.IGNORE_CASE).find(t) ?: return null
        val who = m.groupValues[1].trim()
        if (who.isBlank() || who.lowercase() in setOf("me", "her", "him", "them", "back", "it", "him back", "her back")) return null
        return who to m.groupValues[2].trim()
    }

    fun sounds(a: String, b: String): Boolean {
        if (a.length < 2 || b.length < 2) return a == b
        if (a == b) return true
        if (a[0] != b[0] && !(a[0] in "ck" && b[0] in "ck")) return false
        return soundex(a) == soundex(b) && lev(a, b) <= maxOf(2, maxOf(a.length, b.length) / 2)
    }

    private fun soundex(w: String): String {
        val code = mapOf('b' to '1', 'f' to '1', 'p' to '1', 'v' to '1', 'c' to '2', 'g' to '2', 'j' to '2', 'k' to '2', 'q' to '2',
            's' to '2', 'x' to '2', 'z' to '2', 'd' to '3', 't' to '3', 'l' to '4', 'm' to '5', 'n' to '5', 'r' to '6')
        val s = w.filter { it.isLetter() }; if (s.isEmpty()) return ""
        val out = StringBuilder(); var last = code[s[0]]
        for (ch in s.drop(1)) { val c = code[ch]; if (c != null && c != last) out.append(c); if (ch !in "hw") last = c }
        return (out.toString() + "000").take(3)
    }

    private fun lev(a: String, b: String): Int {
        val d = IntArray(b.length + 1) { it }
        for (i in 1..a.length) { var prev = d[0]; d[0] = i
            for (j in 1..b.length) { val t = d[j]; d[j] = minOf(d[j] + 1, d[j - 1] + 1, prev + if (a[i - 1] == b[j - 1]) 0 else 1); prev = t } }
        return d[b.length]
    }

    /** Name alignment: what speech-to-text wrote, with names spelled the way they are in the contacts ("Danny Ruis"
     *  → "Danny Ruiz", "Sammie" → "the loved one" after text/call/to). Only capitalised words, so ordinary words are never
     *  changed; a full name (first + last both sound right) wins over a single word. */
    fun align(text: String, contacts: List<Contact> = cache): String {
        if (contacts.isEmpty() || text.isBlank()) return text
        val full = contacts.map { it.name.split(" ").filter(String::isNotBlank) }.filter { it.size >= 2 }
        val parts = contacts.flatMap { it.name.split(" ") }.filter { it.length >= 3 && it[0].isLetter() }.distinct()
        val tok = Regex("[A-Za-z'’-]+")
        val ws = tok.findAll(text).toList()
        val out = StringBuilder(); var pos = 0; var i = 0
        fun emit(range: IntRange, repl: String) { out.append(text, pos, range.first).append(repl); pos = range.last + 1 }
        while (i < ws.size) {
            val w = ws[i].value
            if (w[0].isUpperCase() && w.lowercase() !in NOT_NAMES) {
                val nxt = ws.getOrNull(i + 1)
                if (nxt != null && nxt.value[0].isUpperCase() && text.substring(ws[i].range.last + 1, nxt.range.first).isBlank()) {
                    val hit = full.firstOrNull { f -> sounds(w.lowercase(), f[0].lowercase()) && sounds(nxt.value.lowercase(), f.last().lowercase()) }
                    if (hit != null) { emit(ws[i].range.first..nxt.range.last, hit.first() + " " + hit.last()); i += 2; continue }
                }
                val prev = ws.getOrNull(i - 1)?.value?.lowercase()
                val afterVerb = prev in setOf("to", "text", "call", "message", "ring", "tell", "phone", "send", "ask", "remind", "email")
                if (afterVerb && parts.none { it == w }) {
                    val close = parts.filter { p -> sounds(w.lowercase(), p.lowercase()) }
                    if (close.size == 1) { emit(ws[i].range, close[0]); i++; continue }
                }
            }
            i++
        }
        return out.append(text.substring(pos)).toString()
    }

    // What Echo calls someone when he greets them (same rules as echolink/names.py on the Mac):
    // the owner's loved one → the loved one, Neighbor Jo Miller → Jo, Terry (cell) → Terry, Dr. Alex Grant → Dr. Grant,
    // Aunt May → Aunt May, VZ Roadside Assistance / Apple Inc. → "" (a business: no name).
    private val TITLES = setOf("dr", "mr", "mrs", "ms", "miss", "mx", "pastor", "rev", "reverend", "father", "fr", "coach",
        "officer", "judge", "sister", "brother", "professor", "prof", "deacon", "sgt", "captain", "capt")
    private val ABBR = setOf("dr", "mr", "mrs", "ms", "mx", "rev", "fr", "prof", "sgt", "capt")
    private val FAMILY = setOf("aunt", "auntie", "uncle", "grandma", "grandpa", "granny", "nana", "papa", "cousin", "mom",
        "mother", "owner", "father-in-law", "mother-in-law")
    private val DESCRIBERS = setOf("neighbor", "neighbour", "coworker", "co-worker", "work", "boss", "friend", "old", "new",
        "my", "the", "home", "cell", "mobile", "office")
    private val BUSINESS = Regex("\\b(?:inc|llc|ltd|co|corp|company|assistance|roadside|services?|shop|store|pharmacy|" +
        "clinic|dental|dentist|bank|insurance|auto|repair|restaurant|pizza|hospital|school|church|office|support|center|" +
        "centre|world|plumbing|electric|roofing|molding|construction|contractors?|salon|vet|veterinary|hotel|delivery|nailed it)\\b",
        RegexOption.IGNORE_CASE)

    fun greetName(saved: String): String {
        val n = saved.replace(Regex("\\(.*?\\)|\\[.*?]"), " ").replace(Regex("[^\\w.'\\- ]"), " ")
        var words = n.split(Regex("\\s+")).filter { it.isNotBlank() }
        if (words.isEmpty()) return ""
        val w0 = words[0].lowercase().trimEnd('.')
        if (BUSINESS.containsMatchIn(words.joinToString(" ")) && w0 !in TITLES && w0 !in FAMILY) {
            return if (words.size >= 2 && words[1].lowercase().trimEnd('s') in setOf("contractor", "plumber", "electrician", "painter")) words[0] else ""
        }
        if (words.all { it == it.lowercase() }) return ""
        if (w0 in TITLES) {
            val t = if (words[0].endsWith(".") || w0 !in ABBR) words[0] else words[0] + "."
            return if (words.size >= 2) "$t ${words.last()}" else t
        }
        if (w0 in FAMILY) return words.take(2).joinToString(" ")
        while (words.size > 1 && words[0].lowercase() in DESCRIBERS) words = words.drop(1)
        return words[0]
    }

    /** Echo's own texts start "Hello <name>, this is Echo": the name the way he'd greet this contact. */
    fun fixGreeting(body: String, c: Contact): String {
        if (!body.contains("this is Echo", ignoreCase = true)) return body      // the owner's own words stay his
        val g = greetName(c.name)
        return body.replaceFirst(Regex("^(Hello|Hey|Hi)(?: [^,]{1,40})?,"), if (g.isBlank()) "Hello," else "Hello $g,")
    }

    /** The text, with a misheard name ("Hi Sammie") written the way the contact spells it. */
    fun fixName(body: String, heard: String, c: Contact): String {
        val h = heard.trim().split(" ").first(); val real = greetName(c.name).ifBlank { return body }
        if (h.isBlank() || h.equals(real, true)) return body
        return body.replace(Regex("\\b" + Regex.escape(h) + "\\b", RegexOption.IGNORE_CASE), real)
    }

    fun canDial(c: Context) = Intent(Intent.ACTION_DIAL, Uri.parse("tel:1")).resolveActivity(c.packageManager) != null
    fun canSms(c: Context) = Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:1")).resolveActivity(c.packageManager) != null
    fun whatsapp(c: Context) = try { c.packageManager.getPackageInfo("com.whatsapp", 0); true } catch (e: Exception) { false }
    fun messenger(c: Context) = c.packageManager.getLaunchIntentForPackage("com.facebook.orca")

    fun dial(n: String) = Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(n)))
    fun sms(n: String, body: String) = Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + Uri.encode(n))).putExtra("sms_body", body)
    fun wa(n: String, body: String) = Intent(Intent.ACTION_VIEW, Uri.parse("https://wa.me/${n.filter { it.isDigit() }}?text=${Uri.encode(body)}"))
        .setPackage("com.whatsapp")

    private val NOT_NAMES = setOf("i", "me", "my", "a", "an", "the", "to", "and", "her", "him", "them", "it", "send", "text", "call",
        "message", "phone", "ring", "echo", "please", "can", "you", "could", "would", "will", "look", "up", "find", "search", "for",
        "in", "on", "contacts", "contact", "list", "number", "saying", "say", "tell", "that", "let", "know", "hey", "ok", "okay",
        "go", "ahead", "now", "back", "real", "quick", "video", "from", "with", "using", "iphone", "tablet", "mac", "local")
    private val QUESTION = Regex("^(?:what|did|when|who|whose|has|have|had|why|how|is|was|were|does|are)\\b", RegexOption.IGNORE_CASE)
    private val CONTACTS = Regex("\\b(?:contacts?|contact list|phone ?book|phone list|address book)\\b", RegexOption.IGNORE_CASE)
    private val LOOKUP = Regex("\\b(?:search|find|look (?:up|for)|look in|check|pull up|show me|open|do i have)\\b", RegexOption.IGNORE_CASE)
    private val CALLW = Regex("\\b(video call|call|phone|ring)\\b(?!s)", RegexOption.IGNORE_CASE)
    private val TEXTW = Regex("\\b(text|message)\\b", RegexOption.IGNORE_CASE)
    private val BODY = Regex("\\b(?:saying|that says|to say|and say|and tell (?:her|him|them)|tell (?:her|him|them)|and let (?:her|him|them) know|let (?:her|him|them) know)\\b[:,]?\\s*(.+)$|:\\s*(.+)$",
        RegexOption.IGNORE_CASE)

    /** A person named ANYWHERE in what was said: capitalised words (speech-to-text capitalises names) or a contact's
     *  exact name; up to 3 words, longest first. */
    private fun named(t: String, contacts: List<Contact>): Pair<String, List<Contact>>? {
        val words = Regex("[A-Za-z'’-]+").findAll(t).map { it.value }.toList()
        for (k in 3 downTo 1) for (i in 0..words.size - k) {
            val w = words.subList(i, i + k)
            if (w.any { it.lowercase() in NOT_NAMES }) continue
            val cand = w.joinToString(" ").lowercase()
            val exact = contacts.filter { c -> c.name.lowercase() == cand || c.name.lowercase().split(" ").first() == cand }
            if (exact.isNotEmpty()) return cand to exact
            if (w.all { it[0].isUpperCase() } && !(i == 0 && k == 1)) find(cand, contacts).takeIf { it.isNotEmpty() }?.let { return cand to it }
        }
        return null
    }

    /** Anywhere in the sentence: "could you look up the loved one and send her a text saying …", "find Danny in my contacts". */
    fun parseAnywhere(text: String, contacts: List<Contact>): PhoneAction? {
        val t = text.trim().trimEnd('.', '!').replace(Regex("^(?:hey |ok |okay )?echo[,]?\\s+", RegexOption.IGNORE_CASE), "")
            .replace(Regex("^(?:please |can you |could you |would you |will you )+", RegexOption.IGNORE_CASE), "")
        val lookup = CONTACTS.containsMatchIn(t) || Regex("^(?:do i have|find|look up|search for|pull up|show me)\\b", RegexOption.IGNORE_CASE).containsMatchIn(t)
        if (QUESTION.containsMatchIn(t) && !lookup) return null
        // several things at once ("text the loved one … and remind me at 5"): Echo prepares them all, so none is lost
        if (Regex("\\band (?:then )?(?:remind|put|add|schedule|email|e-mail|give|set)\\b", RegexOption.IGNORE_CASE).containsMatchIn(t)) return null
        val (heard, hits) = named(t, contacts) ?: return null
        val call = CALLW.find(t); val txt = TEXTW.find(t)
        val kind = when {
            txt != null && (call == null || txt.range.first < call.range.first) -> "text"
            call != null -> "call"
            lookup && LOOKUP.containsMatchIn(t) || CONTACTS.containsMatchIn(t) -> "show"
            else -> return null
        }
        val body = if (kind != "text") "" else BODY.find(t)?.let { m -> (m.groupValues[1].ifBlank { m.groupValues[2] }).trim().trimEnd('?')
            .replaceFirstChar { it.uppercase() } } ?: ""
        return PhoneAction(kind, hits, hits.singleOrNull()?.let { fixName(body, heard, it) } ?: body, heard)
    }

    /** "call the loved one", "video call Joe", "text the loved one I'm on my way", "send a text to Joe saying …" → what to confirm. */
    fun parse(text: String, contacts: List<Contact>): PhoneAction? {
        // Echo makes the call himself: "call Danny and introduce yourself", "have Echo call the loved one and ask her …"
        if (CALLW.containsMatchIn(text) && ECHO_CALLS.containsMatchIn(text) && !QUESTION.containsMatchIn(text.trim())) {
            named(text, contacts)?.let { (_, hits) -> return PhoneAction("echocall", hits, text.trim()) }
        }
        // passing something on ("tell the loved one that…", "let her know…", "ask him if…"): Echo writes it as himself, so
        // it goes to him (a call is still placed right here)
        if (RELAY.containsMatchIn(text)) return (parseStart(text, contacts) ?: parseAnywhere(text, contacts))?.takeIf { it.kind != "text" }
        return parseStart(text, contacts) ?: parseAnywhere(text, contacts)
    }
    private val ECHO_CALLS = Regex("\\b(?:introduce yourself|on my behalf|for me\\b.*\\b(?:ask|tell)|have echo call|you call|" +
        "call \\S+(?: \\S+)? and (?:ask|tell|see|find out|let|say|check|invite|remind|introduce|thank|wish))", RegexOption.IGNORE_CASE)
    private val RELAY = Regex("\\b(?:tell|let \\w+(?: \\w+)? know|ask|remind|introduce yourself|introduce)\\b", RegexOption.IGNORE_CASE)

    private fun parseStart(text: String, contacts: List<Contact>): PhoneAction? {
        val t = text.trim().trimEnd('.', '!', '?').replace(Regex("^(?:hey |ok |okay )?echo[,]?\\s+", RegexOption.IGNORE_CASE), "")
            .replace(Regex("^(?:please |can you |could you |would you )+", RegexOption.IGNORE_CASE), "")
        if (Regex("\\band (?:then )?(?:remind|call|text|email|e-mail|put|add|schedule|tell|let|give|send)\\b", RegexOption.IGNORE_CASE).containsMatchIn(t)) return null
        val m = Regex("^(?:(video call|call|phone|ring)|(text|message|send (?:a )?(?:text|message) to|send))\\s+(.+)$", RegexOption.IGNORE_CASE)
            .find(t) ?: return null
        val kind = if (m.groupValues[1].isNotBlank()) "call" else "text"
        val words = m.groupValues[3].split(Regex("\\s+"))
        for (k in minOf(3, words.size) downTo 1) {
            val cand = words.take(k).joinToString(" ").trimEnd(',', ':').lowercase()
            val hits = find(cand, contacts)
            if (hits.isNotEmpty()) {
                val body = words.drop(k).joinToString(" ").replace(Regex("^(?:that|saying|to say|and say|and tell (?:her|him|them)|and let (?:her|him|them) know)\\s+",
                    RegexOption.IGNORE_CASE), "").trim().replaceFirstChar { it.uppercase() }
                if (kind == "call" && body.isNotBlank() && !body.matches(Regex("(?i)(on|with|using) .*|now|back"))) return null
                return PhoneAction(kind, hits, if (kind == "text") (hits.singleOrNull()?.let { fixName(body, cand, it) } ?: body) else "", cand)
            }
        }
        return null
    }

    // what was started from Echo (Android doesn't let apps read Meet/Messenger call history; this list is Echo's own)
    fun recent(c: Context): List<JSONObject> = try { val a = JSONArray(c.getSharedPreferences("calls", 0).getString("recent", "[]"))
        (0 until a.length()).map { a.getJSONObject(it) } } catch (e: Exception) { emptyList() }
    fun remember(c: Context, who: String, how: String) {
        val l = listOf(JSONObject().put("who", who).put("how", how).put("at", System.currentTimeMillis())) + recent(c)
        c.getSharedPreferences("calls", 0).edit().putString("recent", JSONArray(l.take(40)).toString()).apply()
    }
}

/** Texts and calls from the owner's own number: the Mac sends them through his iPhone. Only ever called from an
 *  Approve button that shows exactly who (and what) first. Returns null when it worked, else what went wrong. */
object MacLine {
    fun contacts(link: EchoLink): JSONArray? = try {
        link.http.getJson("${link.base}/v1/phone/contacts", link.session).optJSONArray("contacts") } catch (e: Exception) { null }

    private fun post(link: EchoLink?, path: String, body: JSONObject): String? {
        link ?: return "Echo can't be reached right now."
        return try {
            link.http.newCall(okhttp3.Request.Builder().url("${link.base}$path").header("Authorization", "Bearer ${link.session}")
                .post(body.put("approved", true).toString().toRequestBody("application/json".toMediaType()))
                .build()).execute().use { r ->
                if (r.isSuccessful) null else try { JSONObject(r.body!!.string()).let { it.optString("detail").ifBlank { it.optString("error") } } }
                    catch (e: Exception) { "The Mac said no (${r.code})." }
            }
        } catch (e: Exception) { "Echo can't be reached right now." }
    }

    fun text(link: EchoLink?, name: String, number: String, body: String) =
        post(link, "/v1/phone/text", JSONObject().put("name", name).put("number", number).put("text", body))
    fun call(link: EchoLink?, name: String, number: String) =
        post(link, "/v1/phone/call", JSONObject().put("name", name).put("number", number))
}

/** CALLS & TEXTS (the owner). */
@Composable
fun CallsScreen(link: EchoLink?, conn: Conn, hasContacts: Boolean, askContacts: () -> Unit,
                open: (Contact) -> Unit) {
    val ctx = LocalContext.current
    var q by remember { mutableStateOf("") }
    var all by remember { mutableStateOf(Phonebook.cache) }
    var notes by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    LaunchedEffect(hasContacts) { if (hasContacts) all = withContext(Dispatchers.IO) { Phonebook.load(ctx) } }
    LaunchedEffect(conn) {
        if (conn == Conn.CONNECTED && link != null) withContext(Dispatchers.IO) {
            MacLine.contacts(link)?.let { Phonebook.setMac(it); all = Phonebook.cache }
            try { notes = jsonObjects(link.http.getJson("${link.base}/v1/calls/notes", link.session).optJSONArray("notes")) } catch (_: Exception) {}
        }
    }
    Column(Modifier.fillMaxSize().padding(horizontal = 18.dp)) {
        if (!hasContacts) {
            Box(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(CardBg).padding(16.dp)) {
                Column {
                    Text("Echo can also use this tablet's contacts (they stay on this tablet).", color = Cream, fontSize = 17.sp)
                    Button(askContacts, Modifier.padding(top = 8.dp), colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Ink)) {
                        Text("Allow contacts") }
                }
            }
            Spacer(Modifier.height(10.dp))
        }
        OutlinedTextField(q, { q = it }, Modifier.fillMaxWidth(), singleLine = true, placeholder = { Text("Find someone") },
            colors = OutlinedTextFieldDefaults.colors(focusedTextColor = Cream, unfocusedTextColor = Cream, focusedBorderColor = Gold,
                unfocusedBorderColor = Color(0xFF3A3020)))
        Text("Or just tell Echo: “call the loved one” or “text the loved one I'm on my way”.", color = Muted, fontSize = 15.sp, modifier = Modifier.padding(top = 6.dp))
        val shown = if (q.isBlank()) all.filter { it.starred }.ifEmpty { all.take(30) } else all.filter { it.name.contains(q, true) }
        val recentNames = Phonebook.recent(ctx).map { it.getString("who") }.distinct().take(6)
        LazyColumn(Modifier.fillMaxSize().padding(top = 10.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            if (q.isBlank() && recentNames.isNotEmpty()) {
                item { Text("Recent", color = Gold, fontSize = 18.sp) }
                items(recentNames.mapNotNull { n -> all.firstOrNull { it.name == n } }, key = { "r" + it.id }) { c -> ContactRow(c) { open(c) } }
            }
            if (q.isBlank()) item { Text(if (all.any { it.starred }) "Favorites" else "People", color = Gold, fontSize = 18.sp) }
            items(shown, key = { "c" + it.id }) { c -> ContactRow(c) { open(c) } }
            if (q.isNotBlank() && shown.isEmpty()) item { Text("Nobody called “$q” in your contacts.", color = Muted, fontSize = 16.sp) }
            if (q.isBlank() && notes.isNotEmpty()) {
                item { Text("Notes after calls", color = Gold, fontSize = 18.sp, modifier = Modifier.padding(top = 10.dp)) }
                items(notes.take(20), key = { "n" + it.optString("id") }) { n ->
                    Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(CardBg).padding(12.dp)) {
                        Text("${n.optString("who")} · ${n.optString("at")}", color = Muted, fontSize = 14.sp)
                        Text(n.optString("text"), color = Cream, fontSize = 16.sp)
                    }
                }
            }
            item {
                Text("Calls and texts go out from your own number: the Mac sends them through your iPhone, and only after you " +
                    "tap Approve. People come from the Mac's Contacts (turn on iCloud Contacts on the iPhone to get all of them) " +
                    "and this tablet's.",
                    color = Muted, fontSize = 14.sp, modifier = Modifier.padding(vertical = 14.dp))
            }
        }
    }
}

@Composable
private fun ContactRow(c: Contact, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(CardBg).clickable { onClick() }.padding(12.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(44.dp).clip(CircleShape).background(Color(0xFF3A3020)), contentAlignment = Alignment.Center) {
            Text(c.name.take(1).uppercase(), color = GoldLight, fontSize = 20.sp)
        }
        Text(c.name, color = Cream, fontSize = 18.sp, modifier = Modifier.padding(start = 14.dp).weight(1f))
        if (c.starred) Text("★", color = Gold, fontSize = 18.sp)
    }
}

/** One person: every way this device can reach them, plus Echo's help. Nothing happens without a tap. */
@Composable
fun ContactCard(c: Contact, close: () -> Unit, prep: () -> Unit, after: () -> Unit, writeText: () -> Unit,
                textNow: () -> Unit, together: () -> Unit = {}, link: EchoLink? = null) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var confirm by remember { mutableStateOf<Phone?>(null) }
    var busy by remember { mutableStateOf("") }
    fun go(i: Intent, how: String) {
        try { ctx.startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)); Phonebook.remember(ctx, c.name, how) }
        catch (e: Exception) { android.widget.Toast.makeText(ctx, "That app couldn't open it.", android.widget.Toast.LENGTH_LONG).show() }
    }
    Overlay(close) {
        Text(c.name, color = Cream, fontSize = 26.sp)
        Spacer(Modifier.height(10.dp))
        val p0 = confirm
        if (p0 != null) {
            Text("Call ${c.name}?", color = Cream, fontSize = 22.sp)
            Text("${p0.label} ${p0.number}", color = GoldLight, fontSize = 20.sp, modifier = Modifier.padding(top = 4.dp))
            Text("It goes out from your iPhone number, through the Mac. The call rings on the Mac and your iPhone.",
                color = Muted, fontSize = 15.sp, modifier = Modifier.padding(top = 6.dp))
            if (busy.isNotBlank()) Text(busy, color = Warn, fontSize = 16.sp, modifier = Modifier.padding(top = 6.dp))
            Row(Modifier.padding(top = 12.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button({
                    busy = "Calling…"
                    scope.launch {
                        val err = withContext(Dispatchers.IO) { MacLine.call(link, c.name, p0.number) }
                        if (err == null) { Phonebook.remember(ctx, c.name, "call (iPhone)"); busy = ""; confirm = null; close()
                            android.widget.Toast.makeText(ctx, "Calling ${c.name}…", android.widget.Toast.LENGTH_LONG).show() }
                        else busy = err
                    }
                }, colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Ink)) { Text("Approve call", fontSize = 18.sp) }
                TextButton({ confirm = null; busy = "" }) { Text("Cancel", color = Gold, fontSize = 18.sp) }
            }
            return@Overlay
        }
        c.phones.forEach { p -> Act("Call ${p.label} ${p.number} from my iPhone") { confirm = p } }
        val dial = Phonebook.canDial(ctx)
        c.phones.forEach { p -> if (dial) Act("Call ${p.label} ${p.number}") { go(Phonebook.dial(p.number), "call") } }
        c.apps.forEach { a -> Act(a.label) { go(a.intent, a.label) } }
        if (c.phones.isNotEmpty()) Act("Text ${c.name}") { textNow() }
        Phonebook.messenger(ctx)?.let { m -> Act("Open Messenger (pick ${c.name.split(" ").first()} there)") { go(m, "Messenger") } }
        if (!dial && c.apps.isEmpty()) Text("No calling app on this tablet knows ${c.name}. Google Meet can call people who use it; " +
            "add them in Meet first.", color = Warn, fontSize = 15.sp, modifier = Modifier.padding(top = 6.dp))
        Text("With Echo", color = Gold, fontSize = 18.sp, modifier = Modifier.padding(top = 16.dp))
        Act("Get ready for the call with Echo") { prep() }
        Act("Echo joins the call (on speaker)") { together() }
        Act("Write a text with Echo") { writeText() }
        Act("Notes after the call") { after() }
    }
}

/** Check the text, then hand it to Messages / WhatsApp. The message app shows it once more; you tap Send there. */
@Composable
fun TextCard(c: Contact?, choices: List<Contact>, body0: String, close: () -> Unit, pick: (Contact) -> Unit,
             echoWrite: (Contact, String) -> Unit, link: EchoLink? = null) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var body by remember(body0) { mutableStateOf(body0) }
    var to by remember(c) { mutableStateOf(c?.phones?.firstOrNull { it.label.contains("mobile", true) || it.label.contains("cell", true) } ?: c?.phones?.firstOrNull()) }
    var busy by remember { mutableStateOf("") }
    Overlay(close) {
        if (c == null) {
            Text("Which one?", color = Cream, fontSize = 24.sp)
            choices.forEach { x -> Act(x.name) { pick(x) } }
            return@Overlay
        }
        Text("Text ${c.name}", color = Cream, fontSize = 24.sp)
        OutlinedTextField(body, { body = it }, Modifier.fillMaxWidth().padding(top = 10.dp).height(140.dp),
            placeholder = { Text("What do you want to say?") },
            colors = OutlinedTextFieldDefaults.colors(focusedTextColor = Cream, unfocusedTextColor = Cream, focusedBorderColor = Gold,
                unfocusedBorderColor = Color(0xFF3A3020)))
        val num = c.phones.firstOrNull()?.number
        if (c.phones.size > 1) Row(Modifier.padding(top = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            c.phones.forEach { p -> Text("${p.label} ${p.number}", color = if (p == to) Ink else Cream, fontSize = 15.sp,
                modifier = Modifier.clip(RoundedCornerShape(10.dp)).background(if (p == to) Gold else Color(0xFF2A241C)).clickable { to = p }
                    .padding(horizontal = 10.dp, vertical = 8.dp)) }
        }
        val t0 = to
        if (t0 != null) {
            Text("To ${c.name} · ${t0.number} · from your iPhone number", color = GoldLight, fontSize = 16.sp, modifier = Modifier.padding(top = 8.dp))
            if (busy.isNotBlank()) Text(busy, color = Warn, fontSize = 16.sp, modifier = Modifier.padding(top = 6.dp))
            Button({
                busy = "Sending…"
                scope.launch {
                    val err = withContext(Dispatchers.IO) { MacLine.text(link, c.name, t0.number, body) }
                    if (err == null) { Phonebook.remember(ctx, c.name, "text (iPhone)"); busy = ""; close()
                        android.widget.Toast.makeText(ctx, "Sent to ${c.name}.", android.widget.Toast.LENGTH_LONG).show() }
                    else busy = err
                }
            }, Modifier.padding(top = 10.dp).fillMaxWidth(), enabled = body.isNotBlank() && busy != "Sending…",
                colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Ink)) { Text("Approve & send", fontSize = 19.sp) }
        }
        fun go(i: Intent, how: String) {
            try { ctx.startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)); Phonebook.remember(ctx, c.name, how); close() }
            catch (e: Exception) { android.widget.Toast.makeText(ctx, "That app couldn't open it.", android.widget.Toast.LENGTH_LONG).show() }
        }
        if (num == null) Text("${c.name} has no phone number in Contacts.", color = Warn, fontSize = 15.sp)
        else {
            if (Phonebook.canSms(ctx)) Act("Send with Messages (you tap Send there)") { go(Phonebook.sms(num, body), "text") }
            if (Phonebook.whatsapp(ctx)) Act("Send with WhatsApp") { go(Phonebook.wa(num, body), "WhatsApp") }
            c.apps.filter { it.label.contains("message", true) }.forEach { a -> Act(a.label) { go(a.intent, a.label) } }

        }
        Act(if (body.isBlank()) "Let Echo write it" else "Let Echo improve it") { echoWrite(c, body) }
    }
}

@Composable
private fun Act(t: String, onClick: () -> Unit) = Text(t, color = Cream, fontSize = 18.sp,
    modifier = Modifier.padding(top = 8.dp).fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(Color(0xFF2A241C))
        .border(1.dp, Color(0xFF3A3020), RoundedCornerShape(12.dp)).clickable { onClick() }.padding(horizontal = 16.dp, vertical = 14.dp))

/** What Echo just said, kept on screen above any approval box (the owner: the words first, never hidden by the box). */
object EchoSaid { var text by mutableStateOf("") }

@Composable
fun SaidHeader() {
    val t = EchoSaid.text
    if (t.isNotBlank()) Text(t, color = Cream, fontSize = 19.sp, lineHeight = 26.sp, modifier = Modifier.widthIn(max = 620.dp)
        .fillMaxWidth().padding(horizontal = 20.dp, vertical = 10.dp).clip(RoundedCornerShape(14.dp)).background(Color(0xFF1A1611))
        .padding(14.dp))
}

@Composable
private fun Overlay(close: () -> Unit, content: @Composable () -> Unit) {
    Column(Modifier.fillMaxSize().background(Color(0xD0000000)).clickable { close() }.padding(top = 16.dp),
        horizontalAlignment = Alignment.CenterHorizontally) {
        SaidHeader()
        Column(Modifier.widthIn(max = 560.dp).fillMaxWidth().padding(horizontal = 20.dp).clip(RoundedCornerShape(18.dp)).background(Color(0xFF15110D))
            .border(1.dp, Color(0xFF3A3020), RoundedCornerShape(18.dp)).clickable(enabled = false) {}.verticalScroll(rememberScrollState())
            .padding(20.dp)) {
            content()
            TextButton(close, Modifier.align(Alignment.End)) { Text("Close", color = Gold) }
        }
    }
}
