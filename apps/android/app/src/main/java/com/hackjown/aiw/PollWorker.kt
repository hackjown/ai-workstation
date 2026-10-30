package com.hackjown.aiw

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import java.net.HttpURLConnection
import java.net.URL

/**
 * Background check of the server's notification list (the same rows the web bell and the iOS app's /ws/events
 * "notification" events come from). Shows a local notification for the kinds that need the user:
 *   input   = takeover / question / form,  confirm = a confirmation,  failed = the task failed.
 * Tapping it opens the task (deep path task/<id>, routed by the web app's ?open=). Nothing is marked read on the
 * server, so the web console's bell is unchanged.
 */
class PollWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val c = applicationContext
        if (!Prefs.configured(c) || !Prefs.notifyEnabled(c)) return@withContext Result.success()
        val rows = try { fetch(c) } catch (e: Exception) { return@withContext Result.success() }  // retry next period
        val last = Prefs.lastSeen(c)
        val now = System.currentTimeMillis() / 1000.0
        // first poll after setup: only what arrived in the last 20 minutes, not the whole history
        val since = if (last < 0) now - 20 * 60 else last
        var newest = last.coerceAtLeast(since)
        val fresh = mutableListOf<org.json.JSONObject>()
        for (i in 0 until rows.length()) {
            val n = rows.optJSONObject(i) ?: continue
            val at = n.optDouble("created_at", 0.0)
            if (at > newest) newest = at
            if (at <= since) continue
            if (n.optString("kind") !in KINDS) continue
            fresh.add(n)
        }
        Prefs.setLastSeen(c, newest)
        fresh.sortBy { it.optDouble("created_at", 0.0) }
        for (n in fresh.takeLast(5)) show(c, n)
        Result.success()
    }

    private fun fetch(c: Context): JSONArray {
        val u = URL(Prefs.server(c) + "/api/notifications?unread=true")
        val h = u.openConnection() as HttpURLConnection
        h.connectTimeout = 10_000; h.readTimeout = 15_000
        val t = Prefs.token(c)
        if (t.isNotEmpty()) h.setRequestProperty("Authorization", "Bearer $t")
        h.setRequestProperty("Accept", "application/json")
        try {
            if (h.responseCode != 200) throw IllegalStateException("HTTP ${h.responseCode}")
            return JSONArray(h.inputStream.bufferedReader().use { it.readText() })
        } finally { h.disconnect() }
    }

    companion object {
        val KINDS = setOf("input", "confirm", "failed")

        fun deepPath(n: org.json.JSONObject): String {
            val tid = n.optString("task_id", "")
            return if (tid.isNotEmpty() && tid != "null") "task/$tid" else "chat"
        }

        fun show(c: Context, n: org.json.JSONObject) {
            if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(c, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) return
            val id = n.optString("id", n.optString("created_at"))
            val open = Intent(c, MainActivity::class.java)
                .setAction("com.hackjown.aiw.OPEN." + id)
                .putExtra(MainActivity.EXTRA_OPEN, deepPath(n))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            val pi = PendingIntent.getActivity(c, id.hashCode(), open,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
            val title = n.optString("title").ifEmpty { "Aiw" }
            val body = n.optString("body", "")
            val b = NotificationCompat.Builder(c, AiwApp.CHANNEL)
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(NotificationCompat.BigTextStyle().bigText(body))
                .setAutoCancel(true)
                .setContentIntent(pi)
                .setCategory(NotificationCompat.CATEGORY_MESSAGE)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setWhen((n.optDouble("created_at", 0.0) * 1000).toLong().takeIf { it > 0 } ?: System.currentTimeMillis())
                .setShowWhen(true)
            try { NotificationManagerCompat.from(c).notify(id.hashCode(), b.build()) } catch (_: SecurityException) {}
        }
    }
}
