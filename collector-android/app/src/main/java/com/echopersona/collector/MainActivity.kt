package com.echopersona.collector

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.MediaMetadataRetriever
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import android.app.TimePickerDialog
import android.graphics.BitmapFactory
import android.os.Build
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.video.FallbackStrategy
import androidx.camera.video.FileOutputOptions
import androidx.camera.video.Quality
import androidx.camera.video.QualitySelector
import androidx.camera.video.Recorder
import androidx.camera.video.Recording
import androidx.camera.video.VideoCapture
import androidx.camera.video.VideoRecordEvent
import androidx.camera.view.PreviewView
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import kotlinx.coroutines.delay
import org.json.JSONObject
import java.io.File

private val Bg = Color(0xFF07090C); private val Panel = Color(0xFF0E1217); private val Accent = Color(0xFF7FB8D6)
private val Good = Color(0xFF8FCFA8); private val Warn = Color(0xFFC9A86A); private val Rec = Color(0xFFD97B7B)
private val TextC = Color(0xFFD9E1E8); private val Dim = Color(0xFF7C8894)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = Store(this)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(primary = Accent, background = Bg, surface = Panel,
                onSurface = TextC, onBackground = TextC)) {
                Surface(Modifier.fillMaxSize(), color = Bg) { App(store) }
            }
        }
    }

    fun keepScreenOn(on: Boolean) {
        if (on) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }
}

@Composable
fun App(store: Store) {
    var screen by remember { mutableStateOf(if (store.consent() == null) "consent" else "home") }
    var refresh by remember { mutableIntStateOf(0) }
    when (screen) {
        "consent" -> Consent(store) { screen = "home" }
        "home" -> Home(store, refresh) { screen = "session" }
        "session" -> SessionScreen(store) { refresh++; screen = "home" }
    }
}

@Composable
fun Consent(store: Store, done: () -> Unit) {
    var name by remember { mutableStateOf("") }
    Column(Modifier.fillMaxSize().padding(24.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        Text("ECHO COLLECTOR", color = Accent, letterSpacing = 3.sp, fontSize = 14.sp)
        Text("Before we start", fontSize = 26.sp, color = TextC)
        Text(Store.CONSENT_TEXT, color = TextC, fontSize = 16.sp)
        Text("Recording only happens when you press Record, while this screen is open. A red banner shows whenever the camera or microphone is on. This app has no internet access.", color = Dim, fontSize = 14.sp)
        OutlinedTextField(name, { name = it }, label = { Text("Type your full name to agree") }, modifier = Modifier.fillMaxWidth())
        Button({ store.giveConsent(name); done() }, enabled = name.isNotBlank(), modifier = Modifier.fillMaxWidth()) { Text("I agree") }
    }
}

@Composable
fun Home(store: Store, refresh: Int, start: () -> Unit) {
    val ctx = LocalContext.current
    val recovered = remember(refresh) { store.recoverOpenSessions() }
    var times by remember { mutableStateOf(Reminders.times(ctx)) }
    val askNotify = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
    LaunchedEffect(Unit) {
        Reminders.schedule(ctx)
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) askNotify.launch(Manifest.permission.POST_NOTIFICATIONS)
    }
    fun pick(which: Int) {
        val cur = if (which == 1) times.first else times.second
        TimePickerDialog(ctx, { _, h, m ->
            val v = h * 60 + m
            times = if (which == 1) v to times.second else times.first to v
            Reminders.setTimes(ctx, times.first, times.second)
        }, cur / 60, cur % 60, false).show()
    }
    val s = remember(refresh, recovered) { store.summary() }
    var paused by remember { mutableStateOf(store.paused) }
    var free by remember { mutableStateOf(store.freeSpaceAfterCopy) }
    var target by remember { mutableIntStateOf(store.targetDays) }
    var freed by remember { mutableStateOf<Int?>(null) }
    Column(Modifier.fillMaxSize().padding(24.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Text("ECHO COLLECTOR", color = Accent, letterSpacing = 3.sp, fontSize = 14.sp)
        Text("Day ${minOf(s.days + if (s.todayCount == 0) 1 else 0, target)} of $target", fontSize = 30.sp, color = TextC)
        Text("Today: ${s.todayCount} of 2 sessions", color = if (s.todayCount >= 2) Good else Dim)
        if (recovered > 0) Text("Saved $recovered session(s) that were left open. Nothing was lost.", color = Good, fontSize = 13.sp)
        LinearProgressIndicator(progress = { (s.days.toFloat() / target).coerceIn(0f, 1f) }, modifier = Modifier.fillMaxWidth(), color = Good)
        Card(colors = CardDefaults.cardColors(containerColor = Panel)) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Stat("Sessions recorded", "${s.sessions} (${s.completed} complete)")
                Stat("Minutes kept", "%.1f".format(s.keptMinutes))
                Stat("Stored on this phone", "%.0f MB".format(s.bytes / 1e6))
                Stat("Waiting to copy to the Mac", "${s.waiting} session(s)")
                Stat("Copied and verified by the Mac", "${s.transferred} session(s)")
            }
        }
        Button(start, enabled = !paused, modifier = Modifier.fillMaxWidth().height(56.dp)) {
            Text(if (paused) "Collection paused" else "Start a session (about 15 min)", fontSize = 17.sp)
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Switch(paused, { paused = it; store.paused = it }); Spacer(Modifier.width(10.dp)); Text("Pause collection", color = TextC)
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Reminders", color = TextC, modifier = Modifier.weight(1f))
            OutlinedButton({ pick(1) }) { Text(Reminders.label(times.first)) }
            Spacer(Modifier.width(6.dp))
            OutlinedButton({ pick(2) }) { Text(Reminders.label(times.second)) }
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Collection length", color = TextC, modifier = Modifier.weight(1f))
            for (d in listOf(14, 28)) FilterChip(target == d, { target = d; store.targetDays = d }, { Text("$d days") }, Modifier.padding(start = 6.dp))
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Switch(free, { free = it; store.freeSpaceAfterCopy = it }); Spacer(Modifier.width(10.dp))
            Text("Free up space after the Mac has verified a copy", color = TextC)
        }
        if (free) OutlinedButton({ freed = store.freeVerifiedSpace() }) { Text("Free up space now") }
        freed?.let { Text("Removed $it verified recording(s) from the phone. The Mac keeps the originals.", color = Good, fontSize = 13.sp) }
        Text("To copy recordings to the Mac: plug this phone into the Mac with a USB cable, then click \"Copy from phone\" in Echo's Voice Studio.",
            color = Dim, fontSize = 13.sp)
    }
}

@Composable
fun Stat(label: String, value: String) = Row { Text(label, color = Dim, modifier = Modifier.weight(1f)); Text(value, color = TextC) }

private fun keptTaskIds(store: Store): Set<String> {
    val out = HashSet<String>()
    File(store.root, "sessions").listFiles()?.forEach { d ->
        val m = File(d, "manifest.json"); if (!m.exists()) return@forEach
        val t = JSONObject(m.readText()).getJSONArray("takes")
        for (i in 0 until t.length()) t.getJSONObject(i).takeIf { it.optString("status") == "kept" }?.let { out += it.getString("task_id") }
    }
    return out
}

@SuppressLint("MissingPermission")
@Composable
fun SessionScreen(store: Store, exit: () -> Unit) {
    val ctx = LocalContext.current
    val activity = ctx as MainActivity
    val lifecycle = LocalLifecycleOwner.current
    val plan = remember { Plan.session(ctx, keptTaskIds(store)) }
    val session = remember { store.newSession() }
    var i by remember { mutableIntStateOf(0) }
    var recording by remember { mutableStateOf(false) }
    var lastTake by remember { mutableStateOf<Pair<File, Long>?>(null) }   // file, duration: waiting for Keep/Redo
    var elapsed by remember { mutableLongStateOf(0L) }
    var level by remember { mutableFloatStateOf(0f) }
    var error by remember { mutableStateOf<String?>(null) }
    var granted by remember { mutableStateOf(hasPerms(ctx)) }
    val askPerms = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { granted = hasPerms(ctx) }
    LaunchedEffect(Unit) { if (!granted) askPerms.launch(arrayOf(Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO)) }

    val task = plan.getOrNull(i)
    val camera = task != null && task.kind != Kind.VOICE
    val previewView = remember { PreviewView(ctx).apply { scaleType = PreviewView.ScaleType.FILL_CENTER } }
    var videoCapture by remember { mutableStateOf<VideoCapture<Recorder>?>(null) }
    var imageCapture by remember { mutableStateOf<ImageCapture?>(null) }
    var activeRec by remember { mutableStateOf<Recording?>(null) }
    var wav by remember { mutableStateOf<WavRecorder?>(null) }
    var startedAt by remember { mutableLongStateOf(0L) }
    var saving by remember { mutableStateOf(false) }   // video is being finalized; its checksum must wait

    fun finish(completed: Boolean) {
        activeRec?.stop(); wav?.stop(); activity.keepScreenOn(false)
        store.finishSession(session, completed); exit()
    }

    // camera on only for face/answer tasks, only while this screen is showing
    val photo = task?.kind == Kind.PHOTO
    DisposableEffect(camera, photo, granted) {
        var provider: ProcessCameraProvider? = null
        if (camera && granted) {
            val f = ProcessCameraProvider.getInstance(ctx)
            f.addListener({
                provider = f.get()
                val preview = Preview.Builder().build().also { it.setSurfaceProvider(previewView.surfaceProvider) }
                try {
                    provider?.unbindAll()
                    if (photo) {   // stills at the sensor's full resolution
                        val ic = ImageCapture.Builder().setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
                            .setResolutionSelector(ResolutionSelector.Builder()
                                .setResolutionStrategy(ResolutionStrategy.HIGHEST_AVAILABLE_STRATEGY).build()).build()
                        provider?.bindToLifecycle(lifecycle, CameraSelector.DEFAULT_FRONT_CAMERA, preview, ic)
                        imageCapture = ic
                    } else {
                        val recorder = Recorder.Builder().setQualitySelector(
                            QualitySelector.from(Quality.FHD, FallbackStrategy.lowerQualityOrHigherThan(Quality.HD))).build()
                        val vc = VideoCapture.withOutput(recorder)
                        provider?.bindToLifecycle(lifecycle, CameraSelector.DEFAULT_FRONT_CAMERA, preview, vc)
                        videoCapture = vc
                    }
                } catch (e: Exception) { error = "Camera unavailable: ${e.message}" }
            }, ContextCompat.getMainExecutor(ctx))
        }
        onDispose { provider?.unbindAll(); videoCapture = null; imageCapture = null }
    }
    LaunchedEffect(recording) {
        activity.keepScreenOn(recording)
        while (recording) { elapsed = System.currentTimeMillis() - startedAt; level = wav?.level ?: 0f; delay(100) }
    }

    BackHandler { if (!recording) finish(false) }   // Back ends the session properly (never mid-recording)
    if (task == null) { LaunchedEffect(Unit) { finish(true) }; return }

    fun takePhoto() {
        error = null
        val ic = imageCapture ?: run { error = "Camera is still starting, try again in a second."; return }
        val f = store.nextTakeFile(session, "jpg")
        saving = true
        ic.takePicture(ImageCapture.OutputFileOptions.Builder(f).build(), ContextCompat.getMainExecutor(ctx),
            object : ImageCapture.OnImageSavedCallback {
                override fun onImageSaved(r: ImageCapture.OutputFileResults) { saving = false; lastTake = f to 0L }
                override fun onError(e: ImageCaptureException) { saving = false; error = "Couldn't take the photo (${e.imageCaptureError})." }
            })
    }
    fun startRec() {
        error = null
        if (task.kind == Kind.VOICE) {
            val f = store.nextTakeFile(session, "wav")
            wav = WavRecorder(f).also { it.start() }
            lastTake = f to 0L
        } else {
            val vc = videoCapture ?: run { error = "Camera is still starting, try again in a second."; return }
            val f = store.nextTakeFile(session, "mp4")
            activeRec = vc.output.prepareRecording(ctx, FileOutputOptions.Builder(f).build()).withAudioEnabled()
                .start(ContextCompat.getMainExecutor(ctx)) { ev ->
                    if (ev is VideoRecordEvent.Finalize) {
                        saving = false
                        if (ev.hasError()) error = "Recording problem (${ev.error})."
                    }
                }
            lastTake = f to 0L
        }
        startedAt = System.currentTimeMillis(); elapsed = 0; recording = true
    }
    fun stopRec() {
        val dur = System.currentTimeMillis() - startedAt
        wav?.stop(); wav = null
        if (activeRec != null) saving = true
        activeRec?.stop(); activeRec = null
        recording = false
        lastTake = lastTake?.first?.let { it to dur }
    }
    fun decide(keep: Boolean) {
        val (f, dur) = lastTake ?: return
        if (saving) return
        store.saveTakeMeta(f, task, dur, captureInfo(f, task.kind), keep)
        lastTake = null
        if (keep) i++
    }

    Column(Modifier.fillMaxSize()) {
        if (recording) Box(Modifier.fillMaxWidth().background(Rec).padding(10.dp), contentAlignment = Alignment.Center) {
            Text(if (camera) "● RECORDING — camera and microphone on" else "● RECORDING — microphone on", color = Color.White, fontWeight = FontWeight.Bold)
        }
        Column(Modifier.weight(1f).padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("Step ${i + 1} of ${plan.size} · ${task.category.replace('_', ' ')}", color = Accent, fontSize = 13.sp, letterSpacing = 1.sp)
            Text(task.title, color = Dim, fontSize = 15.sp)
            Text(task.text, color = TextC, fontSize = if (task.kind == Kind.VOICE) 26.sp else 21.sp, lineHeight = 32.sp)
            if (camera) {
                if (granted) AndroidView({ previewView }, Modifier.fillMaxWidth().weight(1f))
                else Text("Camera permission is needed for this step.", color = Warn)
            } else {
                Spacer(Modifier.weight(1f))
                LinearProgressIndicator(progress = { level.coerceIn(0f, 1f) }, modifier = Modifier.fillMaxWidth().height(10.dp),
                    color = if (level > 0.9f) Rec else Good)
            }
            Text(if (recording) "%d s  (about %d s is plenty)".format(elapsed / 1000, task.seconds)
                 else if (saving) "Saving…" else if (lastTake != null) "Keep this take, or record it again?" else if (photo) "Press Take photo when you're ready." else "Press Record when you're ready.",
                color = Dim)
            error?.let { Text(it, color = Warn) }
        }
        Row(Modifier.fillMaxWidth().padding(16.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            when {
                recording -> Button({ stopRec() }, Modifier.weight(1f).height(56.dp), colors = ButtonDefaults.buttonColors(containerColor = Rec)) { Text("Stop") }
                lastTake != null -> {
                    Button({ decide(true) }, Modifier.weight(1f).height(56.dp), enabled = !saving, colors = ButtonDefaults.buttonColors(containerColor = Good)) { Text("Keep & next") }
                    OutlinedButton({ decide(false) }, Modifier.weight(1f).height(56.dp), enabled = !saving) { Text("Redo") }
                }
                photo -> {
                    Button({ takePhoto() }, Modifier.weight(1f).height(56.dp), enabled = granted && !saving) { Text("Take photo") }
                    OutlinedButton({ i++ }, Modifier.height(56.dp)) { Text("Skip") }
                }
                else -> {
                    Button({ startRec() }, Modifier.weight(1f).height(56.dp), enabled = granted) { Text("Record") }
                    OutlinedButton({ i++ }, Modifier.height(56.dp)) { Text("Skip") }
                    OutlinedButton({ finish(false) }, Modifier.height(56.dp)) { Text("End") }
                }
            }
        }
    }
}

private fun hasPerms(ctx: Context) = listOf(Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO)
    .all { ContextCompat.checkSelfPermission(ctx, it) == PackageManager.PERMISSION_GRANTED }

private fun captureInfo(f: File, kind: Kind): JSONObject = JSONObject().apply {
    if (kind == Kind.PHOTO) {
        val o = BitmapFactory.Options().apply { inJustDecodeBounds = true }; BitmapFactory.decodeFile(f.absolutePath, o)
        put("format", "jpeg"); put("camera", "front"); put("width", o.outWidth); put("height", o.outHeight)
    } else if (kind == Kind.VOICE) {
        put("format", "wav/pcm_s16le"); put("sample_rate", 48000); put("channels", 1)
        put("capture", JSONObject().put("source", "MIC").put("processing", "none"))
    } else {
        put("format", "mp4/h264+aac"); put("camera", "front")
        try {
            MediaMetadataRetriever().use { m ->
                m.setDataSource(f.absolutePath)
                put("width", m.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_WIDTH)?.toIntOrNull() ?: JSONObject.NULL)
                put("height", m.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_HEIGHT)?.toIntOrNull() ?: JSONObject.NULL)
                put("rotation", m.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_ROTATION)?.toIntOrNull() ?: 0)
                put("frame_rate", m.extractMetadata(MediaMetadataRetriever.METADATA_KEY_CAPTURE_FRAMERATE)?.toFloatOrNull() ?: JSONObject.NULL)
            }
        } catch (_: Exception) { }
    }
}
