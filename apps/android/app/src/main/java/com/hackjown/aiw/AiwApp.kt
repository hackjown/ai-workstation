package com.hackjown.aiw

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit

class AiwApp : Application() {
    override fun onCreate() {
        super.onCreate()
        val ch = NotificationChannel(CHANNEL, getString(R.string.channel_name), NotificationManager.IMPORTANCE_HIGH)
        ch.description = "接管、确认、任务失败"
        getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
        schedulePoll(this)
    }

    companion object {
        const val CHANNEL = "aiw_tasks"
        private const val WORK = "aiw-notify-poll"

        /** A light poll of GET /api/notifications every 15 minutes (WorkManager's minimum), only with a network. */
        fun schedulePoll(c: Context, replace: Boolean = false) {
            val wm = WorkManager.getInstance(c)
            if (!Prefs.configured(c) || !Prefs.notifyEnabled(c)) { wm.cancelUniqueWork(WORK); return }
            val req = PeriodicWorkRequestBuilder<PollWorker>(15, TimeUnit.MINUTES)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build()
            wm.enqueueUniquePeriodicWork(WORK,
                if (replace) ExistingPeriodicWorkPolicy.CANCEL_AND_REENQUEUE else ExistingPeriodicWorkPolicy.KEEP, req)
        }
    }
}
