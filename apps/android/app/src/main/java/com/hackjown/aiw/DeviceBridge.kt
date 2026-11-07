package com.hackjown.aiw

import android.Manifest
import android.annotation.SuppressLint
import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationManager
import android.net.Uri
import android.os.Build
import android.provider.AlarmClock
import android.provider.CalendarContract
import android.provider.ContactsContract
import android.util.Log
import androidx.core.content.ContextCompat
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.records.ExerciseSessionRecord
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.HydrationRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.WeightRecord
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import androidx.health.connect.client.units.Mass
import androidx.health.connect.client.units.Volume
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZonedDateTime
import java.util.TimeZone
import java.util.UUID
import java.util.concurrent.TimeUnit

/**
 * v3.9.27 device bridge (server: services/api/aiw_api/device_bridge.py), the Android twin of iOS DeviceBridge.swift.
 *   WS /ws/device?token=…  hello{device_id, platform:"android", capabilities} -> call{id, tool, args} -> result{id, ok, result|error}
 * Only capabilities switched on in DeviceActivity are announced. Writes are approved on the server's confirm card first.
 * The socket lives while MainActivity is visible; calls made meanwhile wait on the server (TTL) and a push asks to open the app.
 */
class DeviceBridge private constructor(private val ctx: Context) {
    companion object {
        private const val TAG = "aiw.device"
        val CAPS = listOf(
            "calendar.read" to "读取日历", "calendar.write" to "新建日程", "contacts.read" to "搜索通讯录",
            "health.read" to "读取健康（Health Connect：步数、睡眠、心率、运动）", "health.write" to "写入健康（饮水、体重）",
            "location.read" to "当前位置", "alarm.write" to "设闹钟（系统闹钟 App）")
        @SuppressLint("StaticFieldLeak") @Volatile private var inst: DeviceBridge? = null
        fun get(c: Context): DeviceBridge = inst ?: synchronized(this) { inst ?: DeviceBridge(c.applicationContext).also { inst = it } }
    }

    private val sp = ctx.getSharedPreferences("aiw_device", Context.MODE_PRIVATE)
    private val http = OkHttpClient.Builder().pingInterval(25, TimeUnit.SECONDS).build()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var ws: WebSocket? = null
    private var alive = false
    private var backoff = 2L
    @Volatile var online = false; private set
    @Volatile var lastError = ""; private set

    val deviceId: String get() = sp.getString("device_id", null) ?: UUID.randomUUID().toString().also { sp.edit().putString("device_id", it).apply() }
    fun enabled(cap: String) = sp.getBoolean("cap_$cap", false)
    fun setEnabled(cap: String, on: Boolean) { sp.edit().putBoolean("cap_$cap", on).apply(); if (alive) hello() else start() }

    fun start() {
        if (alive || !Prefs.configured(ctx) || CAPS.none { enabled(it.first) }) return
        alive = true; connect()
    }
    fun stop() { alive = false; ws?.close(1000, "background"); ws = null; online = false }

    private fun connect() {
        if (!alive) return
        val base = Prefs.server(ctx).replaceFirst("http", "ws")
        val url = "$base/ws/device?token=" + Uri.encode(Prefs.token(ctx))
        ws = http.newWebSocket(Request.Builder().url(url).build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) { hello() }
            override fun onMessage(webSocket: WebSocket, text: String) { handle(JSONObject(text)) }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) { retry(t.message ?: "连接失败") }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) { if (code == 4401) lastError = "Token 不对" else retry(reason) }
        })
    }
    private fun retry(why: String) {
        online = false; lastError = why
        if (!alive) return
        val wait = backoff; backoff = minOf(backoff * 2, 60)
        scope.launch { kotlinx.coroutines.delay(wait * 1000); connect() }
    }
    private fun hello() {
        val caps = JSONArray(CAPS.map { it.first }.filter { enabled(it) })
        ws?.send(JSONObject().put("type", "hello").put("device_id", deviceId).put("platform", "android")
            .put("name", Build.MODEL).put("capabilities", caps)
            .put("features", JSONObject().put("health_connect", healthAvailable()).put("sdk", Build.VERSION.SDK_INT)).toString())
    }
    private fun handle(m: JSONObject) {
        when (m.optString("type")) {
            "welcome" -> { online = true; backoff = 2; lastError = "" }
            "call" -> {
                val id = m.optString("id"); val tool = m.optString("tool"); val args = m.optJSONObject("args") ?: JSONObject()
                scope.launch {
                    val res = try { JSONObject().put("type", "result").put("id", id).put("ok", true).put("result", run(tool, args)) }
                    catch (e: Exception) { Log.w(TAG, "tool $tool", e); JSONObject().put("type", "result").put("id", id).put("ok", false).put("error", e.message ?: e.javaClass.simpleName) }
                    ws?.send(res.toString())
                }
            }
        }
    }

    // ------------------------------------------------------------------ tools
    private fun need(cap: String, vararg perms: String) {
        if (!enabled(cap)) throw IllegalStateException("用户没有在手机上打开「$cap」")
        for (p in perms) if (ContextCompat.checkSelfPermission(ctx, p) != PackageManager.PERMISSION_GRANTED)
            throw IllegalStateException("没有系统权限 $p：打开 Aiw › 服务器设置 › 设备能力 授权")
    }
    private fun time(v: String?): Long? {
        if (v.isNullOrEmpty()) return null
        return try { OffsetDateTime.parse(v).toInstant().toEpochMilli() } catch (e: Exception) {
            try { java.time.LocalDateTime.parse(v).atZone(ZoneId.systemDefault()).toInstant().toEpochMilli() } catch (e2: Exception) {
                try { java.time.LocalDate.parse(v).atStartOfDay(ZoneId.systemDefault()).toInstant().toEpochMilli() } catch (e3: Exception) { null } } }
    }
    private fun iso(ms: Long) = Instant.ofEpochMilli(ms).toString()

    suspend fun run(tool: String, a: JSONObject): JSONObject = when (tool) {
        "calendar_list" -> { need("calendar.read", Manifest.permission.READ_CALENDAR); calendarList(a) }
        "calendar_create" -> { need("calendar.write", Manifest.permission.WRITE_CALENDAR, Manifest.permission.READ_CALENDAR); calendarCreate(a) }
        "contacts_search" -> { need("contacts.read", Manifest.permission.READ_CONTACTS); contactsSearch(a) }
        "health_read" -> { need("health.read"); healthRead(a) }
        "health_write" -> { need("health.write"); healthWrite(a) }
        "location_get" -> { need("location.read", Manifest.permission.ACCESS_COARSE_LOCATION); locationGet() }
        "alarm_set" -> { need("alarm.write"); alarmSet(a) }
        else -> throw IllegalArgumentException("这台 Android 手机不支持 $tool（提醒事项、快捷指令只在 iOS 上有）")
    }

    private fun calendarList(a: JSONObject): JSONObject {
        val start = time(a.optString("start")) ?: System.currentTimeMillis()
        val end = time(a.optString("end")) ?: (start + 7 * 86400_000L)
        val uri = CalendarContract.Instances.CONTENT_URI.buildUpon().also { ContentUris.appendId(it, start); ContentUris.appendId(it, end) }.build()
        val q = a.optString("query").lowercase()
        val out = JSONArray()
        ctx.contentResolver.query(uri, arrayOf(CalendarContract.Instances.EVENT_ID, CalendarContract.Instances.TITLE,
            CalendarContract.Instances.BEGIN, CalendarContract.Instances.END, CalendarContract.Instances.ALL_DAY,
            CalendarContract.Instances.EVENT_LOCATION, CalendarContract.Instances.CALENDAR_DISPLAY_NAME), null, null,
            CalendarContract.Instances.BEGIN + " ASC")?.use { c ->
            while (c.moveToNext() && out.length() < 100) {
                val title = c.getString(1) ?: ""
                if (q.isNotEmpty() && !title.lowercase().contains(q)) continue
                out.put(JSONObject().put("id", c.getLong(0).toString()).put("title", title).put("start", iso(c.getLong(2)))
                    .put("end", iso(c.getLong(3))).put("all_day", c.getInt(4) == 1).put("location", c.getString(5) ?: "")
                    .put("calendar", c.getString(6) ?: ""))
            }
        }
        return JSONObject().put("events", out)
    }

    private fun calendarCreate(a: JSONObject): JSONObject {
        val start = time(a.optString("start")) ?: throw IllegalArgumentException("start 不是 ISO 时间")
        val end = time(a.optString("end")) ?: throw IllegalArgumentException("end 不是 ISO 时间")
        var calId: Long? = null
        val want = a.optString("calendar")
        ctx.contentResolver.query(CalendarContract.Calendars.CONTENT_URI, arrayOf(CalendarContract.Calendars._ID,
            CalendarContract.Calendars.CALENDAR_DISPLAY_NAME, CalendarContract.Calendars.IS_PRIMARY, CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL),
            null, null, null)?.use { c ->
            while (c.moveToNext()) {
                if (c.getInt(3) < CalendarContract.Calendars.CAL_ACCESS_CONTRIBUTOR) continue
                if (want.isNotEmpty() && c.getString(1) == want) { calId = c.getLong(0); break }
                if (calId == null || c.getInt(2) == 1) calId = c.getLong(0)
            }
        }
        val cid = calId ?: throw IllegalStateException("手机上没有可写的日历")
        val v = ContentValues().apply {
            put(CalendarContract.Events.CALENDAR_ID, cid); put(CalendarContract.Events.TITLE, a.optString("title", "日程"))
            put(CalendarContract.Events.DTSTART, start); put(CalendarContract.Events.DTEND, end)
            put(CalendarContract.Events.EVENT_TIMEZONE, TimeZone.getDefault().id)
            put(CalendarContract.Events.ALL_DAY, if (a.optBoolean("all_day")) 1 else 0)
            if (a.has("location")) put(CalendarContract.Events.EVENT_LOCATION, a.optString("location"))
            if (a.has("notes")) put(CalendarContract.Events.DESCRIPTION, a.optString("notes"))
        }
        val uri = ctx.contentResolver.insert(CalendarContract.Events.CONTENT_URI, v) ?: throw IllegalStateException("写入日历失败")
        return JSONObject().put("created", true).put("id", uri.lastPathSegment ?: "")
    }

    private fun contactsSearch(a: JSONObject): JSONObject {
        val q = a.optString("query"); val limit = a.optInt("limit", 20)
        val out = JSONArray()
        val uri = Uri.withAppendedPath(ContactsContract.CommonDataKinds.Phone.CONTENT_FILTER_URI, Uri.encode(q))
        val seen = HashMap<String, JSONObject>()
        ctx.contentResolver.query(uri, arrayOf(ContactsContract.CommonDataKinds.Phone.CONTACT_ID,
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME, ContactsContract.CommonDataKinds.Phone.NUMBER), null, null, null)?.use { c ->
            while (c.moveToNext() && seen.size < limit) {
                val o = seen.getOrPut(c.getString(0)) { JSONObject().put("name", c.getString(1) ?: "").put("phones", JSONArray()).also { out.put(it) } }
                o.getJSONArray("phones").put(c.getString(2) ?: "")
            }
        }
        return JSONObject().put("contacts", out)
    }

    fun healthAvailable() = HealthConnectClient.getSdkStatus(ctx) == HealthConnectClient.SDK_AVAILABLE
    private fun hc(): HealthConnectClient {
        if (!healthAvailable()) throw IllegalStateException("这台手机没有 Health Connect（Android 14 以下需要先装 Health Connect App）")
        return HealthConnectClient.getOrCreate(ctx)
    }
    private suspend fun healthRead(a: JSONObject): JSONObject {
        val c = hc()
        val start = Instant.ofEpochMilli(time(a.optString("start")) ?: ZonedDateTime.now().toLocalDate().atStartOfDay(ZoneId.systemDefault()).toInstant().toEpochMilli())
        val end = Instant.ofEpochMilli(time(a.optString("end")) ?: System.currentTimeMillis())
        val range = TimeRangeFilter.between(start, end)
        return when (a.optString("kind", "steps")) {
            "steps" -> JSONObject().put("kind", "steps").put("steps",
                c.aggregate(AggregateRequest(setOf(StepsRecord.COUNT_TOTAL), range))[StepsRecord.COUNT_TOTAL] ?: 0L)
            "heart_rate" -> {
                val bpm = c.readRecords(ReadRecordsRequest(HeartRateRecord::class, range)).records.flatMap { r -> r.samples.map { it.beatsPerMinute } }
                JSONObject().put("kind", "heart_rate").put("count", bpm.size).put("min", bpm.minOrNull() ?: 0).put("max", bpm.maxOrNull() ?: 0)
                    .put("avg", if (bpm.isEmpty()) 0.0 else bpm.average())
            }
            "sleep" -> {
                val s = c.readRecords(ReadRecordsRequest(SleepSessionRecord::class, range)).records
                JSONObject().put("kind", "sleep").put("asleep_minutes", s.sumOf { java.time.Duration.between(it.startTime, it.endTime).toMinutes() })
                    .put("sessions", JSONArray(s.map { JSONObject().put("start", it.startTime.toString()).put("end", it.endTime.toString()) }))
            }
            "workouts" -> {
                val s = c.readRecords(ReadRecordsRequest(ExerciseSessionRecord::class, range)).records
                JSONObject().put("kind", "workouts").put("workouts", JSONArray(s.map {
                    JSONObject().put("start", it.startTime.toString()).put("minutes", java.time.Duration.between(it.startTime, it.endTime).toMinutes())
                        .put("type", it.exerciseType).put("title", it.title ?: "") }))
            }
            else -> throw IllegalArgumentException("不支持的健康数据类型")
        }
    }
    private suspend fun healthWrite(a: JSONObject): JSONObject {
        val c = hc()
        val at = Instant.ofEpochMilli(time(a.optString("at")) ?: System.currentTimeMillis())
        val off = ZoneId.systemDefault().rules.getOffset(at)
        val v = a.optDouble("value", 0.0)
        when (a.optString("kind")) {
            "water" -> { require(v > 0 && v < 5000) { "饮水量应在 0–5000 ml" }
                c.insertRecords(listOf(HydrationRecord(startTime = at, startZoneOffset = off, endTime = at.plusSeconds(60), endZoneOffset = off, volume = Volume.milliliters(v)))) }
            "weight" -> { require(v > 2 && v < 400) { "体重应在 2–400 kg" }
                c.insertRecords(listOf(WeightRecord(time = at, zoneOffset = off, weight = Mass.kilograms(v)))) }
            else -> throw IllegalArgumentException("Android 只能写入 water / weight")
        }
        return JSONObject().put("saved", true).put("kind", a.optString("kind")).put("value", v)
    }

    @SuppressLint("MissingPermission")
    private fun locationGet(): JSONObject {
        val lm = ctx.getSystemService(Context.LOCATION_SERVICE) as LocationManager
        val best: Location = lm.getProviders(true).mapNotNull { lm.getLastKnownLocation(it) }.maxByOrNull { it.time }
            ?: throw IllegalStateException("暂时拿不到位置（打开定位后再试）")
        return JSONObject().put("lat", best.latitude).put("lng", best.longitude).put("accuracy_m", best.accuracy.toDouble())
            .put("at", iso(best.time)).put("note", "最近一次系统定位")
    }

    private fun alarmSet(a: JSONObject): JSONObject {
        val t = a.optString("time")
        val m = Regex("""(\d{1,2}):(\d{2})""").find(t) ?: throw IllegalArgumentException("time 要是 HH:MM")
        val days = a.optJSONArray("repeat")
        val map = mapOf("sun" to java.util.Calendar.SUNDAY, "mon" to java.util.Calendar.MONDAY, "tue" to java.util.Calendar.TUESDAY,
            "wed" to java.util.Calendar.WEDNESDAY, "thu" to java.util.Calendar.THURSDAY, "fri" to java.util.Calendar.FRIDAY, "sat" to java.util.Calendar.SATURDAY)
        val i = Intent(AlarmClock.ACTION_SET_ALARM).apply {
            putExtra(AlarmClock.EXTRA_HOUR, m.groupValues[1].toInt()); putExtra(AlarmClock.EXTRA_MINUTES, m.groupValues[2].toInt())
            putExtra(AlarmClock.EXTRA_MESSAGE, a.optString("label", "aiw 闹钟")); putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            if (days != null && days.length() > 0) putIntegerArrayListExtra(AlarmClock.EXTRA_DAYS,
                ArrayList((0 until days.length()).mapNotNull { map[days.optString(it).lowercase().take(3)] }))
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        if (i.resolveActivity(ctx.packageManager) == null) throw IllegalStateException("没有能设闹钟的时钟 App")
        ctx.startActivity(i)
        return JSONObject().put("scheduled", true).put("time", "${m.groupValues[1]}:${m.groupValues[2]}")
    }
}
