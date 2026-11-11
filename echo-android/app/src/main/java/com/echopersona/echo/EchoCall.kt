package com.echopersona.echo

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
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
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

private val Cream = Color(0xFFEEE6D6)
private val Muted = Color(0xFFA89F8F)
private val Warn = Color(0xFFE0B060)

/** "Echo, call Danny and introduce yourself": Echo makes the call himself, on the Mac (through the owner's iPhone).
 *  1. Echo plans the opening; the owner sees exactly who, which number and the first words → Approve.
 *  2. Live: what each side says, with End call.  3. Echo's summary (also shown in a small window on the Mac). */
@Composable
fun EchoCallScreen(link: EchoLink?, who: Contact, request: String, business: Boolean = false, close: () -> Unit) {
    val scope = rememberCoroutineScope()
    val number = who.phones.firstOrNull { it.label.contains("mobile", true) || it.label.contains("cell", true) }?.number
        ?: who.phones.firstOrNull()?.number ?: ""
    var plan by remember { mutableStateOf<JSONObject?>(null) }
    var live by remember { mutableStateOf<JSONObject?>(null) }
    var err by remember { mutableStateOf("") }
    var started by remember { mutableStateOf(false) }
    val facts = androidx.compose.runtime.remember { androidx.compose.runtime.mutableStateMapOf<String, String>() }
    var agree by remember { mutableStateOf("") }

    fun call(path: String, body: JSONObject? = null): JSONObject? {
        val lk = link ?: return null
        return try {
            if (body == null) lk.http.getJson("${lk.base}$path", lk.session) else lk.http.postJson("${lk.base}$path", body, lk.session)
        } catch (e: Exception) { err = e.message ?: "Echo can't be reached right now."; null }
    }

    LaunchedEffect(who.name) {
        if (number.isBlank()) { err = "${who.name} has no phone number in your contacts."; return@LaunchedEffect }
        plan = withContext(Dispatchers.IO) { call("/v1/echocall/plan", JSONObject().put("name", who.name).put("number", number)
            .put("request", request).put("business", business)) }
        agree = plan?.optString("may_agree") ?: ""
    }
    LaunchedEffect(started) {
        val id = plan?.optString("id") ?: return@LaunchedEffect
        if (!started) return@LaunchedEffect
        while (true) {
            withContext(Dispatchers.IO) { call("/v1/echocall/$id") }?.let { live = it }
            if (live?.optString("state") == "ended") break
            delay(800)
        }
    }

    Column(Modifier.fillMaxSize().background(Color(0xE0000000)).padding(top = 16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        SaidHeader()
        Column(Modifier.widthIn(max = 620.dp).fillMaxWidth().padding(18.dp).clip(RoundedCornerShape(18.dp))
            .background(Color(0xFF15110D)).border(1.dp, Color(0xFF3A3020), RoundedCornerShape(18.dp))
            .verticalScroll(rememberScrollState()).padding(20.dp)) {
            val st = live?.optString("state") ?: ""
            Text(when {
                !started -> "Echo calls ${who.name}"
                st == "dialing" -> "Calling ${who.name}…"
                st == "ringing" -> "Ringing…"
                st == "talking" -> "Echo is talking with ${who.name.split(" ").first()}"
                st == "summary" -> "Writing the summary…"
                else -> "Call with ${who.name.split(" ").first()} ended"
            }, color = Cream, fontSize = 24.sp)
            Text("$number · from your iPhone number, through the Mac", color = Muted, fontSize = 15.sp, modifier = Modifier.padding(top = 4.dp))
            if (err.isNotBlank()) Text(err, color = Warn, fontSize = 16.sp, modifier = Modifier.padding(top = 10.dp))

            if (!started) {
                val p = plan
                if (p == null && err.isBlank()) Text("Echo is getting ready…", color = Muted, fontSize = 17.sp, modifier = Modifier.padding(top = 14.dp))
                if (p != null) {
                    Text("He'll start with:", color = Gold, fontSize = 17.sp, modifier = Modifier.padding(top = 14.dp))
                    Text("“${p.optString("opening")}”", color = Cream, fontSize = 19.sp, modifier = Modifier.padding(top = 4.dp))
                    if (p.optString("kind") == "business") {
                        val list = p.optJSONArray("checklist")
                        if (list != null && list.length() > 0) {
                            Text("He'll find out:", color = Gold, fontSize = 17.sp, modifier = Modifier.padding(top = 12.dp))
                            for (i in 0 until list.length()) Text("• " + list.getString(i), color = Cream, fontSize = 16.sp)
                        }
                        val needs = jsonObjects(p.optJSONArray("needs"))
                        if (needs.isNotEmpty()) {
                            Text("They'll probably ask for (Echo says only what you type here):", color = Gold, fontSize = 17.sp,
                                modifier = Modifier.padding(top = 12.dp))
                            needs.forEach { n ->
                                val k = n.optString("label")
                                androidx.compose.material3.OutlinedTextField(facts[k] ?: "", { facts[k] = it },
                                    Modifier.fillMaxWidth().padding(top = 6.dp), singleLine = true,
                                    label = { Text(k) }, placeholder = { Text(n.optString("why")) },
                                    colors = androidx.compose.material3.OutlinedTextFieldDefaults.colors(focusedTextColor = Cream,
                                        unfocusedTextColor = Cream, focusedBorderColor = Gold, unfocusedBorderColor = Color(0xFF3A3020)))
                            }
                        }
                        Text("He may agree to:", color = Gold, fontSize = 17.sp, modifier = Modifier.padding(top = 12.dp))
                        androidx.compose.material3.OutlinedTextField(agree, { agree = it }, Modifier.fillMaxWidth().padding(top = 4.dp),
                            placeholder = { Text("Nothing: he'll check with you (e.g. “book Tue–Thu after 1 pm”)") },
                            colors = androidx.compose.material3.OutlinedTextFieldDefaults.colors(focusedTextColor = Cream,
                                unfocusedTextColor = Cream, focusedBorderColor = Gold, unfocusedBorderColor = Color(0xFF3A3020)))
                        Text("He waits on hold, gets through phone menus, stays polite, never pays for anything, and texts " +
                            "you the answers afterwards.", color = Muted, fontSize = 15.sp, modifier = Modifier.padding(top = 10.dp))
                    } else Text("Then he listens, answers any questions, and ends with “Thank you for your time, ${who.name.split(" ").first()}. " +
                        "Talk to you soon.” He waits for an answer before hanging up. You can end the call any time.",
                        color = Muted, fontSize = 15.sp, modifier = Modifier.padding(top = 10.dp))
                    Row(Modifier.padding(top = 14.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        Button({
                            scope.launch {
                                val f = JSONObject(); facts.forEach { (k, v) -> if (v.isNotBlank()) f.put(k, v.trim()) }
                                val r = withContext(Dispatchers.IO) { call("/v1/echocall/${p.optString("id")}/start",
                                    JSONObject().put("approved", true).put("facts", f).put("may_agree", agree.trim())) }
                                if (r != null) { live = r; started = true }
                            }
                        }, colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Ink)) { Text("Approve call", fontSize = 19.sp) }
                        TextButton(close) { Text("Cancel", color = Gold, fontSize = 18.sp) }
                    }
                } else TextButton(close, Modifier.align(Alignment.End)) { Text("Close", color = Gold) }
                return@Column
            }

            val lines = live?.optJSONArray("lines")
            Column(Modifier.padding(top = 14.dp).heightIn(max = 420.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                for (i in 0 until (lines?.length() ?: 0)) {
                    val l = lines!!.getJSONObject(i)
                    val w = l.optString("who")
                    if (w == "note") { Text(l.optString("text"), color = Muted, fontSize = 15.sp); continue }
                    Text(if (w == "echo") "Echo" else w, color = if (w == "echo") Gold else GoldLight, fontSize = 14.sp)
                    Text(l.optString("text"), color = Cream, fontSize = 17.sp)
                }
            }
            val texted = live?.optString("result_text") ?: ""
            if (texted.isNotBlank()) {
                Text("Texted to you", color = Gold, fontSize = 18.sp, modifier = Modifier.padding(top = 16.dp))
                Text(texted, color = Cream, fontSize = 17.sp, modifier = Modifier.padding(top = 4.dp))
            }
            val summary = live?.optString("summary") ?: ""
            if (summary.isNotBlank()) {
                Text("Summary", color = Gold, fontSize = 18.sp, modifier = Modifier.padding(top = 16.dp))
                Text(summary, color = Cream, fontSize = 18.sp, modifier = Modifier.padding(top = 4.dp).clip(RoundedCornerShape(12.dp))
                    .background(Color(0xFF1A1611)).padding(12.dp))
                Text("Saved with your call notes.", color = Muted, fontSize = 14.sp, modifier = Modifier.padding(top = 6.dp))
            }
            val err2 = live?.optString("error") ?: ""
            if (err2.isNotBlank()) Text("Problem: $err2", color = Warn, fontSize = 15.sp, modifier = Modifier.padding(top = 8.dp))
            Row(Modifier.padding(top = 14.dp).fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                if (st != "ended" && st != "summary") Button({
                    scope.launch { withContext(Dispatchers.IO) { call("/v1/echocall/${live?.optString("id")}/end", JSONObject()) } }
                }, colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF8A3A2A), contentColor = Cream)) { Text("End call", fontSize = 18.sp) }
                else TextButton(close) { Text("Close", color = Gold, fontSize = 18.sp) }
            }
        }
    }
}

/** Someone who isn't in the contacts ("call Walmart pharmacy"): Echo looks the number up on the internet and shows
 *  where it came from; the owner picks the number and who makes the call. Nothing is dialed until he taps. */
@Composable
fun LookupScreen(link: EchoLink?, what: String, request: String, echoCalls: (Contact, String) -> Unit, close: () -> Unit) {
    val scope = rememberCoroutineScope()
    val ctx = androidx.compose.ui.platform.LocalContext.current
    var found by remember { mutableStateOf<List<JSONObject>?>(null) }
    var err by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf("") }
    LaunchedEffect(what) {
        found = withContext(Dispatchers.IO) {
            try {
                val lk = link ?: throw Exception("Echo can't be reached right now.")
                val r = lk.http.getJson("${lk.base}/v1/phone/lookup?q=" + java.net.URLEncoder.encode(what, "UTF-8"), lk.session)
                listOfNotNull(r.optJSONObject("best")) + jsonObjects(r.optJSONArray("others"))
            } catch (e: Exception) { err = e.message ?: "The search didn't work."; emptyList() }
        }
    }
    Column(Modifier.fillMaxSize().background(Color(0xE0000000)).padding(top = 16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        SaidHeader()
        Column(Modifier.widthIn(max = 620.dp).fillMaxWidth().padding(18.dp).clip(RoundedCornerShape(18.dp))
            .background(Color(0xFF15110D)).border(1.dp, Color(0xFF3A3020), RoundedCornerShape(18.dp))
            .verticalScroll(rememberScrollState()).padding(20.dp)) {
            Text("“$what” isn't in your contacts", color = Cream, fontSize = 22.sp)
            val f = found
            when {
                f == null -> Text("Echo is looking the number up online…", color = Muted, fontSize = 17.sp, modifier = Modifier.padding(top = 12.dp))
                f.isEmpty() -> Text(err.ifBlank { "Echo couldn't find a phone number for “$what” online. Try saying the town too, " +
                    "like “call Pizza Hut in Springfield”." }, color = Warn, fontSize = 17.sp, modifier = Modifier.padding(top = 12.dp))
                else -> {
                    Text("Found online. Check the number, then choose:", color = Muted, fontSize = 15.sp, modifier = Modifier.padding(top = 8.dp))
                    f.forEachIndexed { i, o ->
                        Column(Modifier.padding(top = 12.dp).fillMaxWidth().clip(RoundedCornerShape(12.dp))
                            .background(Color(0xFF1A1611)).padding(12.dp)) {
                            Text("${o.optString("name")}  ${o.optString("number")}", color = if (i == 0) GoldLight else Cream, fontSize = 19.sp)
                            Text("from ${o.optString("source")}: ${o.optString("title")}", color = Muted, fontSize = 14.sp)
                            Row(Modifier.padding(top = 8.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                                val c = Contact(-900L - i, o.optString("name"), false, listOf(Phone(o.optString("number"), "from ${o.optString("source")}")), emptyList())
                                Button({ echoCalls(c, request) }, colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Ink)) {
                                    Text("Echo calls them") }
                                Button({
                                    busy = "Calling…"
                                    scope.launch {
                                        val e = withContext(Dispatchers.IO) { MacLine.call(link, c.name, o.optString("number")) }
                                        if (e == null) { Phonebook.remember(ctx, c.name, "call (iPhone)"); close() } else busy = e
                                    }
                                }, colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF2A241C), contentColor = Cream)) { Text("Call from my iPhone") }
                            }
                        }
                    }
                }
            }
            if (busy.isNotBlank()) Text(busy, color = Warn, fontSize = 16.sp, modifier = Modifier.padding(top = 8.dp))
            TextButton(close, Modifier.align(Alignment.End)) { Text("Close", color = Gold, fontSize = 18.sp) }
        }
    }
}
