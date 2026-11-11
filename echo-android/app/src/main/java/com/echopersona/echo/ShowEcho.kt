package com.echopersona.echo

import android.graphics.Bitmap
import android.graphics.Matrix
import android.util.Base64
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream
import java.util.concurrent.Executors

/** The camera: keeps only the newest frame in memory (nothing saved), and only while Show Echo is open. */
class CameraFrames : Frames {
    override var camera = "back"
    @Volatile var latest: Bitmap? = null
    @Volatile var paused = false
    private var lastAt = 0L

    fun take(b: Bitmap, rotation: Int) {
        val now = System.currentTimeMillis()
        if (paused || now - lastAt < 200) return
        lastAt = now
        latest = if (rotation == 0) b else Bitmap.createBitmap(b, 0, 0, b.width, b.height,
            Matrix().apply { postRotate(rotation.toFloat()) }, true)
    }

    override fun jpeg(maxSide: Int): String? {
        val b = latest ?: return null
        val s = minOf(1f, maxSide.toFloat() / maxOf(b.width, b.height))
        val small = if (s < 1f) Bitmap.createScaledBitmap(b, (b.width * s).toInt(), (b.height * s).toInt(), true) else b
        val out = ByteArrayOutputStream()
        small.compress(Bitmap.CompressFormat.JPEG, 82, out)
        return Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)
    }
}

/** SHOW ECHO (the owner): Echo sees what the camera sees while the owner talks to him. Back camera = what the owner is looking at,
 *  front camera = the owner. Pictures go only with what he says (or the Look button); the camera stops when he leaves. */
@Composable
fun ShowEchoScreen(talk: Talk, conn: Conn, face: ImageBitmap?, tapRing: () -> Unit, end: () -> Unit) {
    val ctx = LocalContext.current
    val owner = LocalLifecycleOwner.current
    val frames = remember { CameraFrames() }
    var back by remember { mutableStateOf(true) }
    var torch by remember { mutableStateOf(false) }
    var paused by remember { mutableStateOf(false) }
    var cam by remember { mutableStateOf<Camera?>(null) }
    var problem by remember { mutableStateOf("") }
    val preview = remember { PreviewView(ctx).apply { scaleType = PreviewView.ScaleType.FILL_CENTER } }
    val worker = remember { Executors.newSingleThreadExecutor() }
    val speaking by talk.speaker.speaking.collectAsState()
    val mic by talk.rec.level.collectAsState()
    val voice by talk.speaker.level.collectAsState()

    DisposableEffect(Unit) {
        talk.frames = frames
        onDispose { talk.frames = null; frames.latest = null; worker.shutdown()
            try { ProcessCameraProvider.getInstance(ctx).get().unbindAll() } catch (_: Exception) {} }
    }
    LaunchedEffect(back, paused) {
        frames.camera = if (back) "back" else "front"; frames.paused = paused
        val f = ProcessCameraProvider.getInstance(ctx)
        f.addListener({
            val provider = f.get()
            provider.unbindAll(); cam = null
            if (paused) return@addListener                     // paused: camera off, Echo keeps the last picture
            val sel = if (back) CameraSelector.DEFAULT_BACK_CAMERA else CameraSelector.DEFAULT_FRONT_CAMERA
            if (!provider.hasCamera(sel)) { problem = "This device has no ${if (back) "back" else "front"} camera."; return@addListener }
            val p = Preview.Builder().build().also { it.surfaceProvider = preview.surfaceProvider }
            val a = ImageAnalysis.Builder()
                .setResolutionSelector(ResolutionSelector.Builder().setResolutionStrategy(
                    ResolutionStrategy(android.util.Size(1280, 960), ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER)).build())
                .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST).build()
            a.setAnalyzer(worker) { img -> try { frames.take(img.toBitmap(), img.imageInfo.rotationDegrees) } catch (_: Exception) {} finally { img.close() } }
            try { cam = provider.bindToLifecycle(owner, sel, p, a); problem = ""
                cam?.cameraControl?.enableTorch(torch && back) } catch (e: Exception) { problem = "The camera couldn't start: ${e.message}" }
        }, ContextCompat.getMainExecutor(ctx))
    }
    LaunchedEffect(torch) { cam?.cameraControl?.enableTorch(torch && back) }

    Box(Modifier.fillMaxSize().background(Color.Black)) {
        AndroidView({ preview }, Modifier.fillMaxSize().pointerInput(Unit) {
            detectTransformGestures { _, _, zoom, _ ->                        // pinch to zoom
                cam?.let { c -> val z = c.cameraInfo.zoomState.value ?: return@let
                    c.cameraControl.setZoomRatio((z.zoomRatio * zoom).coerceIn(z.minZoomRatio, z.maxZoomRatio)) }
            }
        })
        if (paused) Text("Paused: Echo keeps the last picture", color = Color.White, fontSize = 18.sp,
            modifier = Modifier.align(Alignment.Center).clip(RoundedCornerShape(12.dp)).background(Color(0xAA000000)).padding(14.dp))
        // top: what is on, and the way out
        Row(Modifier.fillMaxWidth().padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(if (paused) "● Camera paused" else "● Camera on${if (talk.listening) " · Mic on" else ""}",
                color = if (paused) Color(0xFFE0B060) else Color(0xFFFF6B5A), fontSize = 15.sp,
                modifier = Modifier.clip(RoundedCornerShape(16.dp)).background(Color(0xAA000000)).padding(horizontal = 12.dp, vertical = 6.dp))
            Spacer(Modifier.weight(1f))
            Pill("End") { end() }
        }
        // bottom: captions, controls, and the ring (tap to talk / interrupt)
        Column(Modifier.align(Alignment.BottomCenter).fillMaxWidth().padding(14.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            val words = listOf(talk.you.takeIf { it.isNotBlank() }, talk.echo.takeIf { it.isNotBlank() },
                (problem.ifBlank { talk.note }).takeIf { it.isNotBlank() }).filterNotNull()
            if (words.isNotEmpty()) Column(Modifier.widthIn(max = 640.dp).fillMaxWidth().clip(RoundedCornerShape(14.dp))
                .background(Color(0xB0000000)).padding(12.dp)) {
                if (talk.you.isNotBlank()) Text(talk.you, color = Color(0xFFBFB6A6), fontSize = 15.sp)
                if (talk.echo.isNotBlank()) Text(talk.echo.takeLast(400), color = Color(0xFFF3EBDD), fontSize = 18.sp)
                (problem.ifBlank { talk.note }).takeIf { it.isNotBlank() }?.let { Text(it, color = Color(0xFFE0B060), fontSize = 15.sp) }
            }
            Row(Modifier.padding(top = 12.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Pill(if (back) "Show me" else "Show what I see") { back = !back; torch = false }
                if (back) Pill(if (torch) "Light off" else "Light") { torch = !torch }
                Pill(if (paused) "Resume" else "Pause") { paused = !paused }
                Box(Modifier.size(112.dp).clip(CircleShape).clickable(remember { MutableInteractionSource() }, null) { tapRing() }) {
                    PresenceRing(talk.presence(conn, speaking), if (talk.listening) mic else voice, face, 112.dp)
                }
                Pill("Look") { talk.look() }
            }
        }
    }
}

@Composable
private fun Pill(t: String, onClick: () -> Unit) = Text(t, color = Color.White, fontSize = 16.sp,
    modifier = Modifier.clip(RoundedCornerShape(22.dp)).background(Color(0xAA1A1611)).clickable { onClick() }
        .padding(horizontal = 16.dp, vertical = 11.dp))
