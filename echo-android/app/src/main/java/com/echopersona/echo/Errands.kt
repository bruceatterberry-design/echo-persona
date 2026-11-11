package com.echopersona.echo

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.AlarmClock
import android.provider.CalendarContract
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Locale

/** Something Echo prepared for the owner (core/errands.py). Nothing happens until he taps Approve; the app that does it
 *  (Messages, Calendar, Clock, Gmail, Maps) shows it once more before it is sent or saved. */
data class Errand(val kind: String, val to: String, val body: String, val whenAt: String, val title: String, val place: String,
                  val who: Contact? = null, val number: String = "") {
    companion object {
        fun from(j: JSONObject) = Errand(j.optString("kind"), j.optString("to"), j.optString("body"), j.optString("when"),
            j.optString("title"), j.optString("place")).resolve()
    }

    /** Texts and calls: the real contact and number (one clear match only), and a misheard name fixed in the words. */
    fun resolve(): Errand {
        if (kind != "text" && kind != "call") return this
        val c = Phonebook.find(to).singleOrNull() ?: return this
        val p = c.phones.firstOrNull { it.label.contains("mobile", true) || it.label.contains("cell", true) } ?: c.phones.firstOrNull() ?: return this
        return copy(who = c, number = p.number, body = if (kind == "text") Phonebook.fixGreeting(Phonebook.fixName(body, to, c), c) else body)
    }

    fun label(): String = when (kind) {
        "text" -> "Text ${who?.name ?: to}${if (number.isNotBlank()) " ($number)" else ""}: “$body”"
        "call" -> "Call ${who?.name ?: to}${if (number.isNotBlank()) " ($number)" else ""}"
        "reminder" -> "Remind you${if (whenAt.isNotBlank()) " " + nice(whenAt) else ""}: $body"
        "calendar" -> "Calendar: $title${if (whenAt.isNotBlank()) ", " + nice(whenAt) else " (pick the day and time)"}${if (place.isNotBlank()) " at $place" else ""}"
        "email" -> "Email $to${if (title.isNotBlank()) ": $title" else ""}"
        "directions" -> "Directions to $place"
        else -> "Note: $body"
    }

    fun millis(): Long? = try { SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US).parse(whenAt)?.time } catch (e: Exception) { null }

    private fun nice(w: String) = millis()?.let { SimpleDateFormat("EEE MMM d 'at' h:mm a", Locale.US).format(it) } ?: w
}

object ErrandRunner {
    /** Returns what to show next: a contact card / text card is opened by the caller (needs a contact). */
    fun intentFor(c: Context, e: Errand, email: String?): Intent? = when (e.kind) {
        "reminder", "calendar" -> {
            val at = e.millis()
            val cal = Intent(Intent.ACTION_INSERT, CalendarContract.Events.CONTENT_URI)
                .putExtra(CalendarContract.Events.TITLE, if (e.kind == "reminder") e.body else e.title)
                .putExtra(CalendarContract.Events.EVENT_LOCATION, e.place)
                .putExtra(CalendarContract.Events.HAS_ALARM, true)
                .apply { if (at != null) putExtra(CalendarContract.EXTRA_EVENT_BEGIN_TIME, at)
                    .putExtra(CalendarContract.EXTRA_EVENT_END_TIME, at + if (e.kind == "reminder") 15 * 60_000L else 60 * 60_000L) }
            val today = at != null && Calendar.getInstance().apply { timeInMillis = at }.get(Calendar.DAY_OF_YEAR) ==
                Calendar.getInstance().get(Calendar.DAY_OF_YEAR)
            // a reminder later today: an alarm with the words (Clock); other days and events: the calendar
            if (e.kind == "reminder" && today) Intent(AlarmClock.ACTION_SET_ALARM)
                .putExtra(AlarmClock.EXTRA_HOUR, Calendar.getInstance().apply { timeInMillis = at!! }.get(Calendar.HOUR_OF_DAY))
                .putExtra(AlarmClock.EXTRA_MINUTES, Calendar.getInstance().apply { timeInMillis = at!! }.get(Calendar.MINUTE))
                .putExtra(AlarmClock.EXTRA_MESSAGE, e.body).takeIf { it.resolveActivity(c.packageManager) != null } ?: cal
            else cal
        }
        "email" -> Intent(Intent.ACTION_SENDTO, Uri.parse("mailto:" + Uri.encode(email ?: e.to.takeIf { "@" in it } ?: "")))
            .putExtra(Intent.EXTRA_SUBJECT, e.title).putExtra(Intent.EXTRA_TEXT, e.body)
        "directions" -> Intent(Intent.ACTION_VIEW, Uri.parse("https://www.google.com/maps/dir/?api=1&destination=" + Uri.encode(e.place)))
        else -> null
    }
}

@Composable
fun ErrandCards(items: List<Errand>, approve: (Errand) -> Unit, skip: (Errand) -> Unit) {
    if (items.isEmpty()) return
    Column(Modifier.widthIn(max = 640.dp).fillMaxWidth().padding(top = 8.dp).clip(RoundedCornerShape(16.dp))
        .background(Color(0xFF1A1611)).border(1.dp, Gold.copy(alpha = 0.45f), RoundedCornerShape(16.dp)).padding(12.dp)) {
        Text("Echo prepared — tap to approve each:", color = Gold, fontSize = 16.sp)
        items.forEach { e ->
            Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(e.label(), color = Color(0xFFEEE6D6), fontSize = 17.sp, modifier = Modifier.weight(1f))
                Text("Skip", color = Color(0xFFA89F8F), fontSize = 16.sp, modifier = Modifier.clip(RoundedCornerShape(12.dp))
                    .clickable { skip(e) }.padding(horizontal = 12.dp, vertical = 10.dp))
                Spacer(Modifier.size(6.dp))
                Text("Approve", color = Ink, fontSize = 16.sp, modifier = Modifier.clip(RoundedCornerShape(12.dp))
                    .background(Gold).clickable { approve(e) }.padding(horizontal = 14.dp, vertical = 10.dp))
            }
        }
    }
}
