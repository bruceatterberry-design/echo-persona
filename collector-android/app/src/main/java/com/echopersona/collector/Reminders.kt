package com.echopersona.collector

import android.Manifest
import android.app.AlarmManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import java.util.Calendar

/**
 * Twice-daily session reminders (default 10:00 and 19:00, adjustable). A reminder is skipped when collection is
 * paused, both of today's sessions are done, or the collection period is over. Reminders never start recording.
 */
object Reminders {
    private const val CHANNEL = "sessions"
    private const val PREFS = "collector"

    fun times(ctx: Context): Pair<Int, Int> {     // minutes after midnight
        val p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        return p.getInt("remind1", 10 * 60) to p.getInt("remind2", 19 * 60)
    }

    fun setTimes(ctx: Context, first: Int, second: Int) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putInt("remind1", first).putInt("remind2", second).apply()
        schedule(ctx)
    }

    fun schedule(ctx: Context) {
        val (a, b) = times(ctx)
        val now = Calendar.getInstance()
        val next = listOf(a, b).flatMap { m ->
            (0..1).map { day -> Calendar.getInstance().apply {
                add(Calendar.DAY_OF_YEAR, day); set(Calendar.HOUR_OF_DAY, m / 60); set(Calendar.MINUTE, m % 60)
                set(Calendar.SECOND, 0); set(Calendar.MILLISECOND, 0)
            } }
        }.filter { it.after(now) }.minByOrNull { it.timeInMillis } ?: return
        val pi = PendingIntent.getBroadcast(ctx, 1, Intent(ctx, ReminderReceiver::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        // a 10-minute delivery window: close to the chosen time, and no special exact-alarm permission needed
        ctx.getSystemService(AlarmManager::class.java).setWindow(AlarmManager.RTC_WAKEUP, next.timeInMillis, 10 * 60_000L, pi)
    }

    fun shouldRemind(ctx: Context): Boolean {
        val store = Store(ctx)
        if (store.paused || store.consent() == null) return false
        val s = store.summary()
        return s.todayCount < 2 && (s.days < store.targetDays || s.todayCount > 0)
    }

    fun notify(ctx: Context) {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Session reminders", NotificationManager.IMPORTANCE_DEFAULT))
        val open = PendingIntent.getActivity(ctx, 0, Intent(ctx, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        nm.notify(1, NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("Time for a Echo session")
            .setContentText("About 15 minutes. Open Echo Persona Collector when you're ready.")
            .setContentIntent(open).setAutoCancel(true).build())
    }

    fun label(minutes: Int): String {
        val h = minutes / 60; val m = minutes % 60
        return "%d:%02d %s".format(if (h % 12 == 0) 12 else h % 12, m, if (h < 12) "AM" else "PM")
    }
}

class ReminderReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED && Reminders.shouldRemind(ctx)) Reminders.notify(ctx)
        Reminders.schedule(ctx)   // always line up the next one (also after a restart)
    }
}
