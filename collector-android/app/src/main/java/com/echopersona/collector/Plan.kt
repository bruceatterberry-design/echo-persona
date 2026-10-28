package com.echopersona.collector

import android.content.Context
import org.json.JSONArray

enum class Kind { FACE, VOICE, ANSWER, PHOTO }

/** One step in a session. FACE = front-camera video (with sound); VOICE = read aloud (WAV); ANSWER = own words (video);
 *  PHOTO = a full-resolution still from the front camera (sharper than video on phones with small front sensors). */
data class Task(
    val id: String, val kind: Kind, val category: String, val title: String, val text: String,
    val seconds: Int, val expressionLabel: String? = null,
)

object Plan {
    // Face tasks: what the camera should see. Labels describe the instruction, not a feeling.
    private val FACE = listOf(
        Task("face-straight", Kind.FACE, "face", "Look at the camera", "Look straight at the camera, relaxed, and breathe normally.", 12, "neutral"),
        Task("face-left", Kind.FACE, "face", "Look slightly left", "Turn your head a little to your left, then slowly back to the camera.", 12, "neutral"),
        Task("face-right", Kind.FACE, "face", "Look slightly right", "Turn your head a little to your right, then slowly back to the camera.", 12, "neutral"),
        Task("face-blink", Kind.FACE, "face", "Natural blinking", "Look at the camera and just blink naturally. No need to do anything else.", 10, "neutral"),
        Task("face-smile", Kind.FACE, "face", "A real smile", "Think of something that makes you happy and let yourself smile.", 10, "smiling"),
        Task("face-thoughtful", Kind.FACE, "face", "Thinking", "Imagine working out a tricky problem. Let your face show you're thinking.", 10, "thoughtful"),
        Task("face-concerned", Kind.FACE, "face", "Concern", "Imagine hearing that a friend isn't feeling well.", 10, "concerned"),
        Task("face-surprise", Kind.FACE, "face", "Mild surprise", "Imagine someone just told you some unexpected good news.", 8, "surprised"),
        Task("face-talk-camera", Kind.FACE, "face", "Talk to the camera", "Tell the camera what you did today, looking at it as you talk.", 25, "speaking"),
        Task("face-talk-away", Kind.FACE, "face", "Talk while looking away", "Keep talking about anything, but look around the room as you would in a real conversation.", 20, "speaking"),
        Task("face-head", Kind.FACE, "face", "Natural head movement", "Nod, tilt and turn your head slowly in any direction, like you're listening to someone.", 12, "neutral"),
    )

    private val PHOTO = listOf(
        Task("photo-straight", Kind.PHOTO, "photo", "Photo: straight on", "Hold the phone at eye level, look straight at the camera, relaxed.", 0, "neutral"),
        Task("photo-smile", Kind.PHOTO, "photo", "Photo: gentle smile", "Look at the camera with a gentle, natural smile.", 0, "smiling"),
        Task("photo-left", Kind.PHOTO, "photo", "Photo: turned a little left", "Turn your head a little to your left, eyes on the camera.", 0, "neutral"),
        Task("photo-right", Kind.PHOTO, "photo", "Photo: turned a little right", "Turn your head a little to your right, eyes on the camera.", 0, "neutral"),
    )

    fun prompts(context: Context): List<Task> {
        val arr = JSONArray(context.assets.open("prompts.json").bufferedReader().readText())
        return (0 until arr.length()).map { i ->
            val p = arr.getJSONObject(i)
            val read = p.getBoolean("read")
            Task(p.getString("id"), if (read) Kind.VOICE else Kind.ANSWER, p.getString("category"),
                p.getString("style"), p.getString("text"), if (read) 15 else 90)
        }
    }

    /**
     * About 15 minutes: 5 face tasks, 10 read-aloud prompts, 2 questions in your own words.
     * Prompts already recorded are used last, so the dataset keeps its variety.
     */
    fun session(context: Context, done: Set<String>): List<Task> {
        val all = prompts(context).sortedBy { it.id in done }.shuffledWithin(done)
        val voice = all.filter { it.kind == Kind.VOICE }
            .groupBy { it.category }.values.flatMap { it.take(2) }.shuffled().take(10)
        val answers = all.filter { it.kind == Kind.ANSWER }.take(2)
        val faces = (listOf(FACE.first()) + FACE.drop(1).shuffled().take(4))
        val out = mutableListOf<Task>()
        out += PHOTO.first(); out += PHOTO.drop(1).shuffled().take(1)   // 2 sharp stills per session
        out += faces.take(3); out += voice.take(5); out += answers.take(1)
        out += faces.drop(3); out += voice.drop(5); out += answers.drop(1)
        return out
    }

    private fun List<Task>.shuffledWithin(done: Set<String>): List<Task> =
        filter { it.id !in done }.shuffled() + filter { it.id in done }.shuffled()
}
