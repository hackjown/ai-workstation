package com.hackjown.aiw

import android.Manifest
import android.os.Bundle
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.SwitchCompat
import androidx.health.connect.client.PermissionController
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.ExerciseSessionRecord
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.HydrationRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.WeightRecord

/** v3.9.27 设备能力: which phone features the agent may use (off by default); switching one on asks for its permission. */
class DeviceActivity : AppCompatActivity() {
    private val bridge by lazy { DeviceBridge.get(this) }
    private lateinit var status: TextView
    private val perms = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { refresh() }
    private val healthPerms = registerForActivityResult(PermissionController.createRequestPermissionResultContract()) { refresh() }

    private val healthRead = setOf(HealthPermission.getReadPermission(StepsRecord::class), HealthPermission.getReadPermission(SleepSessionRecord::class),
        HealthPermission.getReadPermission(HeartRateRecord::class), HealthPermission.getReadPermission(ExerciseSessionRecord::class))
    private val healthWrite = setOf(HealthPermission.getWritePermission(HydrationRecord::class), HealthPermission.getWritePermission(WeightRecord::class))

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = getString(R.string.device_title)
        val pad = (16 * resources.displayMetrics.density).toInt()
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad) }
        status = TextView(this).apply { textSize = 14f }
        col.addView(status)
        col.addView(TextView(this).apply {
            text = "新建日程、写健康数据、设闹钟前，都会先在 Aiw 里弹确认卡片。Aiw 不在前台时，Agent 的调用会在服务器排队，推送提醒你打开 App。"
            textSize = 13f; setPadding(0, pad / 2, 0, pad)
        })
        for ((cap, label) in DeviceBridge.CAPS) {
            col.addView(SwitchCompat(this).apply {
                text = label; isChecked = bridge.enabled(cap); setPadding(0, pad / 2, 0, pad / 2)
                setOnCheckedChangeListener { _, on -> bridge.setEnabled(cap, on); if (on) ask(cap); refresh() }
            })
        }
        setContentView(ScrollView(this).apply { addView(col) })
        refresh()
    }

    private fun ask(cap: String) {
        when (cap) {
            "calendar.read", "calendar.write" -> perms.launch(arrayOf(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR))
            "contacts.read" -> perms.launch(arrayOf(Manifest.permission.READ_CONTACTS))
            "location.read" -> perms.launch(arrayOf(Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.ACCESS_FINE_LOCATION))
            "health.read" -> if (bridge.healthAvailable()) healthPerms.launch(healthRead)
            "health.write" -> if (bridge.healthAvailable()) healthPerms.launch(healthWrite)
        }
    }

    private fun refresh() {
        status.text = (if (bridge.online) "已连上服务器" else "未连接" + (if (bridge.lastError.isNotEmpty()) "：${bridge.lastError}" else "")) +
            (if (!bridge.healthAvailable()) "\n这台手机没有 Health Connect，健康相关能力不可用" else "")
    }

    override fun onResume() { super.onResume(); bridge.start(); status.postDelayed({ refresh() }, 1500) }
}
