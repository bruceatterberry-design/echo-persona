package com.echopersona.echo

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlin.coroutines.resume

private val Cream = Color(0xFFEEE6D6)
private val Muted = Color(0xFFA89F8F)

data class Line(val who: String, val text: String)       // who: owner | guest | echo

private val ADDRESSED = Regex("""^\W*(?:(?:hey|hi|ok|okay|so|and|well)\W+)?echo\b|\becho\W*$|,\s*echo\b""", RegexOption.IGNORE_CASE)

/** "ECHO JOINS IN": the owner has a call on speaker (on another device: Android gives the microphone to one app at a time)
 *  and the tablet listens to the room. Echo answers when someone says "Echo…" or the owner taps the ring. Only after the
 *  other person agreed. The words live only on this screen and vanish when it ends; the Mac keeps nothing. */
@Composable
fun TogetherScreen(talk: Talk, conn: Conn, face: ImageBitmap?, name0: String, end: () -> Unit) {
    var name by remember { mutableStateOf(name0) }
    var agreed by remember { mutableStateOf(false) }
    val lines = remember { mutableStateListOf<Line>() }
    var nextIsForEcho by remember { mutableStateOf(false) }
    val speaking by talk.speaker.speaking.collectAsState()
    val mic by talk.rec.level.collectAsState()
    val voice by talk.speaker.level.collectAsState()
    val list = rememberLazyListState()

    DisposableEffect(Unit) {
        talk.onReply = { t -> lines.add(Line("echo", t)) }
        onDispose { talk.onReply = null; talk.rec.finish(); lines.clear() }
    }
    if (!agreed) {
        Column(Modifier.fillMaxSize().padding(28.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Spacer(Modifier.height(30.dp))
            Text("Echo joins in", color = Cream, fontSize = 28.sp)
            Text("Put your call on speaker on your phone, next to this tablet. Echo listens to the room and answers when " +
                "someone says “Echo…”.", color = Muted, fontSize = 17.sp, modifier = Modifier.padding(top = 10.dp).widthIn(max = 600.dp))
            OutlinedTextField(name, { name = it }, Modifier.padding(top = 18.dp).widthIn(max = 420.dp).fillMaxWidth(),
                label = { Text("Who are you talking with?") }, singleLine = true,
                colors = OutlinedTextFieldDefaults.colors(focusedTextColor = Cream, unfocusedTextColor = Cream, focusedBorderColor = Gold,
                    unfocusedBorderColor = Color(0xFF3A3020), focusedLabelColor = Gold, unfocusedLabelColor = Muted))
            Text("First tell ${name.ifBlank { "them" }}: “Echo, my AI, will listen and may talk with us. Nothing is kept.” " +
                "Only start if they agree.", color = Color(0xFFE0B060), fontSize = 17.sp,
                modifier = Modifier.padding(top = 18.dp).widthIn(max = 600.dp))
            Row(Modifier.padding(top = 18.dp)) {
                TextButton(end) { Text("Cancel", color = Muted, fontSize = 17.sp) }
                Button({ if (name.isNotBlank()) agreed = true }, enabled = name.isNotBlank(), modifier = Modifier.height(54.dp),
                    shape = RoundedCornerShape(14.dp), colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Ink)) {
                    Text("${name.ifBlank { "They" }} agreed — start", fontSize = 17.sp) }
            }
        }
        return
    }

    // the listening loop: one utterance at a time, never while Echo talks or thinks (so he doesn't hear himself)
    LaunchedEffect(agreed, conn) {
        if (conn != Conn.CONNECTED) return@LaunchedEffect
        while (isActive) {
            if (talk.speaker.speaking.value || talk.waiting || talk.listening) { delay(250); continue }
            val wav = suspendCancellableCoroutine<ByteArray?> { k -> talk.rec.record { k.resume(it) } }
            if (wav == null || wav.size <= 44) { delay(300); continue }
            val (text, speaker) = withContext(Dispatchers.IO) { talk.sttWho(wav) } ?: continue
            if (text.isBlank()) continue
            val who = if (speaker == "owner") "owner" else "guest"
            lines.add(Line(who, text))
            if (ADDRESSED.containsMatchIn(text) || nextIsForEcho) {
                nextIsForEcho = false
                talk.sendGroup(text, lines.takeLast(12), name, who)
            }
            if (lines.size > 60) lines.removeRange(0, lines.size - 60)
        }
    }
    LaunchedEffect(lines.size) { if (lines.isNotEmpty()) list.animateScrollToItem(lines.size - 1) }

    Column(Modifier.fillMaxSize().padding(horizontal = 20.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("● Listening to the room · $name agreed", color = Color(0xFFFF6B5A), fontSize = 15.sp, modifier = Modifier.weight(1f))
            TextButton(end) { Text("End", color = Gold, fontSize = 18.sp) }
        }
        Box(Modifier.clip(CircleShape).clickable(remember { MutableInteractionSource() }, null) {
            if (speaking) talk.interrupt() else nextIsForEcho = true
        }) { PresenceRing(talk.presence(conn, speaking).let { if (it == Presence.IDLE) Presence.LISTENING else it },
            if (speaking) voice else mic, face, 220.dp) }
        Text(if (nextIsForEcho) "The next thing said goes to Echo" else "Say “Echo…” to bring him in, or tap the ring",
            color = Muted, fontSize = 16.sp, modifier = Modifier.padding(top = 4.dp))
        LazyColumn(Modifier.widthIn(max = 700.dp).fillMaxWidth().weight(1f).padding(top = 10.dp), state = list) {
            items(lines) { l ->
                Text("${when (l.who) { "owner" -> "You"; "echo" -> "Echo"; else -> name }}: ${l.text}",
                    color = when (l.who) { "echo" -> GoldLight; "owner" -> Cream; else -> Color(0xFFBFD3E6) }, fontSize = 18.sp,
                    modifier = Modifier.padding(vertical = 4.dp))
            }
        }
        Text("Who said what is guessed from your voice; anyone else counts as $name. Nothing here is kept.",
            color = Muted, fontSize = 13.sp, modifier = Modifier.padding(vertical = 8.dp))
    }
}
