package com.echopersona.echo

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.core.animateFloat
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.Dp
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.sin

enum class Presence { IDLE, LISTENING, THINKING, SPEAKING, INTERRUPTED, DISCONNECTED, RECONNECTING }

val Gold = Color(0xFFD8B25A)
val GoldLight = Color(0xFFF3D48A)
val GoldDeep = Color(0xFF9A7428)
val Ink = Color(0xFF0D0B09)
// the owner (2026-11-10): thinking = glowing light blue pulsing outwards; receiving/speaking = glowing purple pulsing inwards
val ThinkBlue = Color(0xFF96D2FF)
val ThinkBlueLight = Color(0xFFCDECFF)
val SpeakPurple = Color(0xFFA05AEB)
val SpeakPurpleLight = Color(0xFFD6AAFF)

/** Echo's presence: a gold ring around his face. Everything is drawn on the GPU (no video, no images except the face). */
@Composable
fun PresenceRing(state: Presence, level: Float, face: ImageBitmap?, size: Dp, modifier: Modifier = Modifier) {
    // Its own clock from the display's frames: Android's "Remove animations" (animator scale 0, on the owner's tablet)
    // freezes normal animations, but not this.
    var frameNanos by remember { mutableLongStateOf(0L) }
    LaunchedEffect(Unit) { while (true) withFrameNanos { frameNanos = it } }
    val clock = ((frameNanos / 1_000_000L) % 12000L) / 12000f
    val lv by animateFloatAsState(level, tween(90), label = "level")
    val away = state == Presence.DISCONNECTED || state == Presence.RECONNECTING
    val tone by animateColorAsState(when {
        away -> Color(0xFF6E665A)
        state == Presence.THINKING -> ThinkBlue
        state == Presence.SPEAKING -> SpeakPurple
        else -> Gold
    }, tween(600), label = "tone")
    val toneLight by animateColorAsState(when (state) {
        Presence.THINKING -> ThinkBlueLight
        Presence.SPEAKING -> SpeakPurpleLight
        else -> GoldLight
    }, tween(600), label = "toneLight")
    val toneDeep = Color(tone.red * 0.65f, tone.green * 0.65f, tone.blue * 0.65f, 1f)
    val flash by animateFloatAsState(if (state == Presence.INTERRUPTED) 1f else 0f, tween(180), label = "flash")

    Box(modifier.size(size), contentAlignment = Alignment.Center) {
        Canvas(Modifier.fillMaxSize()) {
            val c = Offset(this.size.width / 2, this.size.height / 2)
            val r0 = min(this.size.width, this.size.height) * 0.36f
            val tau = (2 * PI).toFloat()
            val secs = clock * 12f
            val breathe = sin(secs * tau / 6f)                          // one slow breath every 6 s
            val r = when (state) {
                Presence.IDLE -> r0 * (1f + 0.018f * breathe)
                Presence.LISTENING -> r0 * (1.02f + 0.07f * lv)
                Presence.THINKING -> r0 * (1f + 0.03f * sin(secs * tau / 1.6f))
                Presence.SPEAKING -> r0 * (1.01f + 0.035f * lv)
                Presence.INTERRUPTED -> r0 * (0.96f)
                else -> r0
            }
            val width = r0 * when (state) {
                Presence.LISTENING -> 0.05f + 0.06f * lv
                Presence.SPEAKING -> 0.05f + 0.04f * lv
                Presence.DISCONNECTED -> 0.025f
                else -> 0.045f
            }
            // soft glow behind the ring
            val glow = when {
                away -> 0.10f
                state == Presence.THINKING -> 0.30f + 0.18f * (0.5f + 0.5f * sin(secs * tau / 1.6f))
                state == Presence.SPEAKING -> 0.30f + 0.30f * lv
                else -> 0.22f + 0.25f * lv
            } + 0.12f * flash
            drawCircle(Brush.radialGradient(listOf(tone.copy(alpha = glow), Color.Transparent), c, r * 1.55f),
                r * 1.55f, c)
            when (state) {
                // thinking: glowing light-blue pulses travelling OUTWARDS
                Presence.THINKING -> for (k in 0 until 3) {
                    val p = ((secs / 1.8f + k / 3f) % 1f)
                    val rr = r * (1f + 0.45f * p)
                    drawCircle(tone.copy(alpha = (1f - p) * 0.25f), rr, c, style = Stroke(width * (1.4f - p) * 2.6f))  // halo
                    drawCircle(toneLight.copy(alpha = (1f - p) * 0.55f), rr, c, style = Stroke(width * (1.4f - p)))
                }
                // receiving / speaking: glowing purple pulses travelling INWARDS, stronger when his voice is louder
                Presence.SPEAKING -> for (k in 0 until 3) {
                    val p = ((secs * 0.9f + k / 3f) % 1f)
                    val rr = r * (1.5f - 0.5f * p)
                    val a = p * (0.2f + 0.6f * lv)
                    drawCircle(tone.copy(alpha = a * 0.45f), rr, c, style = Stroke(width * (0.4f + 0.9f * p) * 2.6f))
                    drawCircle(toneLight.copy(alpha = a), rr, c, style = Stroke(width * (0.4f + 0.9f * p)))
                }
                // listening: gold ripples outwards with his voice (unchanged)
                Presence.LISTENING -> for (k in 0 until 3) {
                    val p = ((secs * 0.6f + k / 3f) % 1f)
                    drawCircle(tone.copy(alpha = (1f - p) * (0.15f + 0.45f * lv)), r * (1f + 0.35f * p), c,
                        style = Stroke(width * (1f - p) * 0.8f))
                }
                else -> {}
            }
            // the ring itself: a gold sweep; speaking adds a gentle wave around its edge
            val path = Path()
            val steps = 180
            for (i in 0..steps) {
                val a = i * tau / steps
                val wobble = when (state) {
                    Presence.SPEAKING -> r0 * 0.022f * lv * (sin(a * 5 + secs * 9f) + 0.6f * sin(a * 8 - secs * 13f))
                    Presence.LISTENING -> r0 * 0.012f * lv * sin(a * 6 + secs * 7f)
                    else -> 0f
                }
                val rr = r + wobble
                val x = c.x + rr * cos(a); val y = c.y + rr * sin(a)
                if (i == 0) path.moveTo(x, y) else path.lineTo(x, y)
            }
            path.close()
            val spin = when (state) {
                Presence.THINKING -> secs * 360f / 1.4f
                Presence.RECONNECTING -> secs * 360f / 3f
                else -> secs * 360f / 24f
            }
            val lit = state == Presence.THINKING || state == Presence.SPEAKING
            if (lit) drawPath(path, tone.copy(alpha = 0.35f), style = Stroke(width * 3.2f))        // the ring's own glow
            rotate(spin, c) {
                drawPath(path, Brush.sweepGradient(
                    if (away) listOf(Color(0xFF4A443B), tone, Color(0xFF4A443B))
                    else listOf(toneDeep, toneLight, tone, toneDeep, toneLight, toneDeep), c), style = Stroke(width))
            }
            // thinking: a bright arc travelling around; reconnecting: a small light looking for the way back
            if (state == Presence.THINKING || state == Presence.RECONNECTING) {
                val a = spin * PI.toFloat() / 180f
                val dot = Offset(c.x + r * cos(a), c.y + r * sin(a))
                val spark = if (state == Presence.THINKING) Color.White else GoldLight
                drawCircle(Brush.radialGradient(listOf(spark.copy(alpha = 0.95f), toneLight.copy(alpha = 0.4f), Color.Transparent),
                    dot, width * 2.6f), width * 2.6f, dot)
            }
            if (flash > 0f) drawCircle(Color.White.copy(alpha = 0.25f * flash), r, c, style = Stroke(width * 1.6f))
        }
        val faceSize = size * 0.58f
        if (face != null) {
            Image(face, "Echo", Modifier.size(faceSize).clip(CircleShape).alpha(if (away) 0.55f else 1f),
                contentScale = ContentScale.Crop, alignment = Alignment.Center)
        } else {
            Canvas(Modifier.size(faceSize)) { drawCircle(Color(0xFF1A1611)) }
        }
    }
}
