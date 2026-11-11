package com.echopersona.echo

import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.net.Uri
import android.provider.CalendarContract
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.ime
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Locale

private val PCream = Color(0xFFEEE6D6)
private val PMuted = Color(0xFFA89F8F)
private val PCard = Color(0xFF1A1611)

/** What the side window next to Echo shows: weather, calendar, photos (asked for) or the websites Echo used. */
data class PanelReq(val kind: String, val query: String = "", val items: JSONArray? = null, val title: String = "")

object Panels {
    private val WEATHER = Regex("\\b(?:weather|forecast|temperature|how (?:hot|cold|warm)|rain(?:ing|y)?|snow(?:ing)?|" +
        "storm|umbrella|jacket|sunny|windy|degrees outside)\\b", RegexOption.IGNORE_CASE)
    private val CALENDAR = Regex("\\b(?:calendar|appointments?|schedule|meetings?|events?|what do i have|am i (?:busy|free)|" +
        "what'?s (?:on )?(?:for )?(?:today|tomorrow|this week)|plans (?:today|tomorrow|this week))\\b", RegexOption.IGNORE_CASE)
    private val PHOTOS = Regex("\\b(?:pictures?|photos?|pics)\\b", RegexOption.IGNORE_CASE)

    /** Something the owner asked that has details worth showing next to Echo's spoken answer. */
    fun forAsk(text: String): PanelReq? = when {
        PHOTOS.containsMatchIn(text) -> PanelReq("photos", text)
        WEATHER.containsMatchIn(text) -> PanelReq("weather", text)
        CALENDAR.containsMatchIn(text) -> PanelReq("calendar", text)
        else -> null
    }

    fun calendar(c: Context, days: Int = 14): List<JSONObject> {
        val now = System.currentTimeMillis()
        val uri = CalendarContract.Instances.CONTENT_URI.buildUpon()
        android.content.ContentUris.appendId(uri, now - 60 * 60_000L)
        android.content.ContentUris.appendId(uri, now + days * 86_400_000L)
        val out = mutableListOf<JSONObject>()
        c.contentResolver.query(uri.build(), arrayOf(CalendarContract.Instances.TITLE, CalendarContract.Instances.BEGIN,
            CalendarContract.Instances.END, CalendarContract.Instances.ALL_DAY, CalendarContract.Instances.EVENT_LOCATION),
            null, null, CalendarContract.Instances.BEGIN + " ASC")?.use { q ->
            while (q.moveToNext() && out.size < 40) out += JSONObject().put("title", q.getString(0) ?: "(no title)")
                .put("begin", q.getLong(1)).put("end", q.getLong(2)).put("allDay", q.getInt(3) == 1).put("where", q.getString(4) ?: "")
        }
        return out
    }
}

/** The side window: a title, the details, and a Dismiss button at the bottom. */
@Composable
fun SidePanel(link: EchoLink?, req: PanelReq, hasCalendar: Boolean, askCalendar: () -> Unit, dismiss: () -> Unit,
              modifier: Modifier = Modifier) {
    val ctx = LocalContext.current
    var data by remember(req) { mutableStateOf<JSONObject?>(null) }
    var cal by remember(req) { mutableStateOf<List<JSONObject>?>(null) }
    var err by remember(req) { mutableStateOf("") }
    var big by remember(req) { mutableStateOf<String?>(null) }
    // Only a deliberate tap closes it: not one that lands on Dismiss because the keyboard just opened or closed and
    // the screen moved under the finger, nor one in the first moment after the window appears.
    val openedAt = remember(req) { System.currentTimeMillis() }
    val ime = WindowInsets.ime.getBottom(androidx.compose.ui.platform.LocalDensity.current) > 0
    var movedAt by remember { mutableStateOf(0L) }
    LaunchedEffect(ime) { movedAt = System.currentTimeMillis() }
    LaunchedEffect(req, hasCalendar) {
        err = ""
        withContext(Dispatchers.IO) {
            try {
                val lk = link
                when (req.kind) {
                    "weather" -> data = lk?.http?.getJson("${lk.base}/v1/panel/weather", lk.session) ?: throw Exception("Echo can't be reached right now.")
                    "photos" -> data = lk?.http?.getJson("${lk.base}/v1/panel/photos?q=" + Uri.encode(req.query), lk.session)
                        ?: throw Exception("Echo can't be reached right now.")
                    "calendar" -> if (hasCalendar) cal = Panels.calendar(ctx)
                }
            } catch (e: Exception) { err = e.message ?: "Couldn't load that." }
        }
    }
    Column(modifier.fillMaxHeight().padding(10.dp).clip(RoundedCornerShape(18.dp)).background(Color(0xFF15110D))
        .border(1.dp, Color(0xFF3A3020), RoundedCornerShape(18.dp)).padding(14.dp)) {
        Text(when (req.kind) { "weather" -> "Weather"; "calendar" -> "Your calendar"; "photos", "photo" -> "Pictures"
            else -> req.title.ifBlank { "Details" } }, color = Gold, fontSize = 22.sp)
        Column(Modifier.weight(1f).fillMaxWidth().padding(top = 8.dp).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(8.dp)) {
            if (err.isNotBlank()) Text(err, color = Color(0xFFE0B060), fontSize = 16.sp)
            when (req.kind) {
                "weather" -> data?.let { WeatherView(it) } ?: if (err.isBlank()) Loading() else Unit
                "calendar" -> if (!hasCalendar) {
                    Text("Echo needs to see the tablet's calendar to show your appointments.", color = PCream, fontSize = 17.sp)
                    Button(askCalendar, colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Ink)) { Text("Allow calendar") }
                } else cal?.let { CalendarView(it) } ?: Loading()
                "photo" -> {                                     // one picture Echo is talking about, big
                    val img = rememberPhoto(link, req.query)
                    if (img != null) Image(img, null, Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).clickable { big = req.query },
                        contentScale = ContentScale.FillWidth)
                    else Loading()
                }
                "photos" -> data?.let { d ->
                    val items = jsonObjects(d.optJSONArray("items"))
                    if (items.isEmpty()) Text("No pictures matched that.", color = PMuted, fontSize = 16.sp)
                    items.chunked(2).forEach { row ->
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            row.forEach { p -> PhotoTile(link, p, Modifier.weight(1f)) { big = p.optString("id") } }
                            if (row.size == 1) Box(Modifier.weight(1f))
                        }
                    }
                } ?: if (err.isBlank()) Loading() else Unit
                else -> jsonObjects(req.items).forEach { w ->
                    Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(PCard).clickable {
                        try { ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(w.optString("url"))).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) } catch (_: Exception) {}
                    }.padding(10.dp)) {
                        Text(w.optString("title"), color = PCream, fontSize = 16.sp)
                        Text(Uri.parse(w.optString("url")).host?.removePrefix("www.") ?: "", color = GoldLight, fontSize = 13.sp)
                        if (w.optString("snippet").isNotBlank()) Text(w.optString("snippet"), color = PMuted, fontSize = 14.sp)
                    }
                }
            }
        }
        Button({ if (System.currentTimeMillis() - maxOf(openedAt, movedAt) > 900) dismiss() }, Modifier.fillMaxWidth().padding(top = 10.dp),
            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF2A241C), contentColor = PCream)) { Text("Dismiss", fontSize = 18.sp) }
    }
    big?.let { id -> BigPhoto(link, id) { big = null } }
}

@Composable private fun Loading() = Text("Loading…", color = PMuted, fontSize = 16.sp)

@Composable
private fun WeatherView(w: JSONObject) {
    val now = w.optJSONObject("now") ?: JSONObject()
    Text(w.optString("place"), color = PMuted, fontSize = 15.sp)
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text("${now.optInt("temp")}°", color = PCream, fontSize = 56.sp)
        Column(Modifier.padding(start = 12.dp)) {
            Text(now.optString("sky"), color = PCream, fontSize = 20.sp)
            Text("Feels ${now.optInt("feels")}° · wind ${now.optInt("wind")} mph · ${now.optInt("humidity")}% humid", color = PMuted, fontSize = 14.sp)
        }
    }
    Text("Next hours", color = GoldLight, fontSize = 16.sp)
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        jsonObjects(w.optJSONArray("hours")).take(6).forEach { h ->
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(h.optString("time"), color = PMuted, fontSize = 13.sp)
                Text("${h.optInt("temp")}°", color = PCream, fontSize = 17.sp)
                if (h.optInt("rain") >= 20) Text("${h.optInt("rain")}%", color = Color(0xFF8FB8E0), fontSize = 12.sp)
            }
        }
    }
    Text("Coming days", color = GoldLight, fontSize = 16.sp)
    jsonObjects(w.optJSONArray("days")).forEach { d ->
        Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).background(PCard).padding(horizontal = 10.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Text(d.optString("day"), color = PCream, fontSize = 16.sp, modifier = Modifier.weight(0.6f))
            Text(d.optString("sky"), color = PMuted, fontSize = 15.sp, modifier = Modifier.weight(1.4f))
            if (d.optInt("rain") >= 20) Text("${d.optInt("rain")}% ", color = Color(0xFF8FB8E0), fontSize = 14.sp)
            Text("${d.optInt("hi")}° / ${d.optInt("lo")}°", color = PCream, fontSize = 16.sp)
        }
    }
}

@Composable
private fun CalendarView(events: List<JSONObject>) {
    if (events.isEmpty()) { Text("Nothing on the calendar for the next two weeks.", color = PMuted, fontSize = 16.sp); return }
    val day = SimpleDateFormat("EEE, MMM d", Locale.US); val time = SimpleDateFormat("h:mm a", Locale.US)
    var last = ""
    events.forEach { e ->
        val d = day.format(e.optLong("begin"))
        if (d != last) { Text(d, color = GoldLight, fontSize = 16.sp, modifier = Modifier.padding(top = 4.dp)); last = d }
        Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).background(PCard).padding(10.dp)) {
            Text(e.optString("title"), color = PCream, fontSize = 17.sp)
            Text((if (e.optBoolean("allDay")) "All day" else time.format(e.optLong("begin")) + " – " + time.format(e.optLong("end"))) +
                (if (e.optString("where").isNotBlank()) " · " + e.optString("where") else ""), color = PMuted, fontSize = 14.sp)
        }
    }
}

@Composable
private fun PhotoTile(link: EchoLink?, p: JSONObject, modifier: Modifier, open: () -> Unit) {
    val img = rememberPhoto(link, p.optString("id"))
    Column(modifier.clip(RoundedCornerShape(10.dp)).background(PCard).clickable { open() }) {
        if (img != null) Image(img, null, Modifier.fillMaxWidth().aspectRatio(1f), contentScale = ContentScale.Crop)
        else Box(Modifier.fillMaxWidth().aspectRatio(1f))
        Text(p.optString("caption").take(60) + (if (p.optString("taken").isNotBlank()) " · " + p.optString("taken").take(4) else ""),
            color = PMuted, fontSize = 12.sp, modifier = Modifier.padding(6.dp))
    }
}

@Composable
private fun BigPhoto(link: EchoLink?, id: String, close: () -> Unit) {
    val img = rememberPhoto(link, id)
    Box(Modifier.fillMaxSize().background(Color(0xF0000000)).clickable { close() }, contentAlignment = Alignment.Center) {
        if (img != null) Image(img, null, Modifier.fillMaxSize().padding(16.dp), contentScale = ContentScale.Fit)
    }
}

private val photoCache = mutableMapOf<String, ImageBitmap>()

@Composable
private fun rememberPhoto(link: EchoLink?, id: String): ImageBitmap? {
    var img by remember(id) { mutableStateOf(photoCache[id]) }
    LaunchedEffect(id) {
        if (img != null || link == null) return@LaunchedEffect
        img = withContext(Dispatchers.IO) {
            try {
                link.http.newCall(okhttp3.Request.Builder().url("${link.base}/v1/panel/photo/$id")
                    .header("Authorization", "Bearer ${link.session}").build()).execute().use { r ->
                    if (!r.isSuccessful) null else r.body!!.bytes().let { b ->
                        BitmapFactory.decodeByteArray(b, 0, b.size, BitmapFactory.Options().apply { inSampleSize = 2 })?.asImageBitmap()
                    }
                }
            } catch (e: Exception) { null }
        }?.also { photoCache[id] = it }
    }
    return img
}
