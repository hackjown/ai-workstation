package com.hackjown.aiw

import android.Manifest
import androidx.appcompat.app.AlertDialog
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.UUID

/**
 * v3.9.28 手机快通道, on-device fast answers (the Android twin of iOS PhoneFast.swift). The chat is the web page
 * (apps/web/home.js): before sending, hSend asks AiwAndroid.fastTry(text); true = handled here.
 *   打电话给X / 给X打电话 / 拨打X / call X  -> contacts looked up here; several numbers -> a picker; always a confirm
 *                                          dialog, then ACTION_CALL (CALL_PHONE granted) or ACTION_DIAL
 *   今天日程 / 今天有什么安排                 -> CalendarContract, answered here
 *   查看提醒事项                            -> not here (Android has no system reminders): the server answers
 * The result goes back to the page (window.aiwFastResult), which shows it and POSTs /api/chat/device_result with
 * client_id. No match, or the permission is not granted -> false, and the message goes to the server as before.
 */
class FastPath(private val act: MainActivity, private val deliverJs: (cid: String, payload: String) -> Unit) {
    sealed class FastIntent {
        data class Call(val who: String) : FastIntent()
        object Reminders : FastIntent()
        object CalendarToday : FastIntent()
    }
    data class Candidate(val name: String, val number: String, val user: String)

    private val bridge get() = DeviceBridge.get(act)

    companion object {
        private val callRes = listOf(
            Regex("""^(?:请|帮我|麻烦|麻烦你)?(?:打电话|打个电话|拨电话|拨个电话|拨打|呼叫)给?\s*(.+)$"""),
            Regex("""^(?:请|帮我|麻烦|麻烦你)?给\s*(.+?)\s*(?:打|拨)(?:个|一个|一下)?电话(?:吧)?$"""),
            Regex("""^(?:please\s+)?(?:call|dial|phone)\s+(.+)$""", RegexOption.IGNORE_CASE))
        private val reminderRes = listOf(
            Regex("""^(?:请|帮我)?(?:查看|看看|看一下|查一下|查查|列出|显示|打开)?(?:一下)?(?:我的)?(?:手机)?(?:上|里)?的?(?:提醒事项|提醒)(?:列表)?(?:有哪些|有什么)?(?:吗|呢)?$"""),
            Regex("""^我(?:现在|还)?有(?:什么|哪些|啥)(?:提醒事项|提醒|待办)(?:吗|呢)?$"""),
            Regex("""^(?:show|list|what are)\s+(?:my\s+)?reminders$""", RegexOption.IGNORE_CASE))
        private val calendarRes = listOf(
            Regex("""^(?:请|帮我)?(?:查看|看看|看一下|查一下)?(?:我)?今天(?:的)?(?:日程|安排|行程|日历)(?:是什么|有哪些|有什么|呢)?$"""),
            Regex("""^(?:我)?今天(?:都)?有(?:什么|哪些|啥)(?:安排|日程|行程|会|会议|事)(?:吗|呢)?$"""),
            Regex("""^(?:what'?s|show)\s+(?:on\s+)?(?:my\s+)?(?:calendar|schedule|agenda)(?:\s+for)?\s+today$""", RegexOption.IGNORE_CASE))

        fun match(raw: String): FastIntent? {
            val t = raw.trim().trim('。', '.', '!', '！', '?', '？', '~', '～', ' ', '，', ',')
            if (t.isEmpty() || t.length > 40 || t.contains('\n') || t.startsWith("【")) return null
            for (re in callRes) {
                val w = re.find(t)?.groupValues?.getOrNull(1)?.trim(' ', '。', '.', '!', '！', '吧', '呀', '啊') ?: continue
                if (w.isNotEmpty() && w.length <= 20 && !w.contains("电话")) return FastIntent.Call(w)
            }
            if (reminderRes.any { it.matches(t) }) return FastIntent.Reminders
            if (calendarRes.any { it.matches(t) }) return FastIntent.CalendarToday
            return null
        }

        /** a typed number (digits and + - ( ) * # only) is dialled as it is */
        fun directNumber(who: String): String? {
            if (who.count { it in '0'..'9' } < 3) return null
            if (who.any { !(it in '0'..'9' || it in " +-()*#") }) return null
            return who.filter { it in '0'..'9' || it in "+*#" }
        }
    }

    /** Called on the JavaBridge thread; true = this app answers (the page clears its input and waits for aiwFastResult). */
    fun tryHandle(text: String): Boolean {
        val intent = match(text) ?: return false
        return when (intent) {
            is FastIntent.Reminders -> false   // no system reminders on Android: the server handles it
            is FastIntent.CalendarToday -> {
                if (!bridge.granted(Manifest.permission.READ_CALENDAR)) return false
                val r = try { bridge.calendarToday() } catch (e: Exception) { return false }
                deliver(text, calendarText(r), "calendar_today", true, JSONObject(), r, null)
                true
            }
            is FastIntent.Call -> {
                val direct = directNumber(intent.who)
                if (direct != null) { act.runOnUiThread { confirm(Candidate("", direct, text)) }; return true }
                if (!bridge.granted(Manifest.permission.READ_CONTACTS)) return false
                val list = try { lookup(intent.who, text) } catch (e: Exception) { return false }
                when {
                    list.isEmpty() -> false
                    list.size == 1 -> { act.runOnUiThread { confirm(list[0]) }; true }
                    else -> { act.runOnUiThread { pick(list) }; true }
                }
            }
        }
    }

    private fun lookup(name: String, user: String): List<Candidate> {
        val r = bridge.contactsSearch(JSONObject().put("query", name).put("limit", 20))
        val arr = r.optJSONArray("contacts") ?: JSONArray()
        val out = mutableListOf<Candidate>()
        val seen = HashSet<String>()
        for (i in 0 until arr.length()) {
            val c = arr.optJSONObject(i) ?: continue
            val phones = c.optJSONArray("phones") ?: continue
            for (j in 0 until phones.length()) {
                val n = phones.optString(j)
                val key = n.filter { it in '0'..'9' }
                if (n.isEmpty() || !seen.add(key)) continue
                out.add(Candidate(c.optString("name"), n, user))
            }
        }
        return out
    }

    private fun pick(list: List<Candidate>) {
        if (act.isFinishing) return
        AlertDialog.Builder(act).setTitle("打给哪一个？")
            .setItems(list.map { "${it.name}  ${it.number}" }.toTypedArray()) { _, i -> confirm(list[i]) }
            .setNegativeButton("取消", null).show()
    }

    private fun confirm(c: Candidate) {
        if (act.isFinishing) return
        AlertDialog.Builder(act).setTitle("拨打电话？")
            .setMessage(if (c.name.isEmpty()) c.number else "${c.name}  ${c.number}")
            .setPositiveButton("拨打") { _, _ -> call(c) }
            .setNegativeButton("取消", null).show()
    }

    private fun call(c: Candidate) {
        val args = JSONObject().put("number", c.number)
        if (c.name.isNotEmpty()) args.put("name", c.name)
        val who = if (c.name.isEmpty()) c.number else "${c.name}（${c.number}）"
        try {
            val r = bridge.phoneCall(args)
            val reply = if (r.optBoolean("dialer_only")) "已打开拨号盘：$who（按拨号键拨出）" else "已在手机上拨打 $who"
            deliver(c.user, reply, "phone_call", true, args, r, null)
        } catch (e: Exception) {
            deliver(c.user, "没能拨打 $who：${e.message}", "phone_call", false, args, null, e.message ?: "失败")
        }
    }

    private fun deliver(user: String, reply: String, tool: String, ok: Boolean, args: JSONObject, result: JSONObject?, error: String?) {
        val p = JSONObject().put("user", user).put("reply", reply).put("tool", tool).put("ok", ok).put("args", args)
            .put("device_id", bridge.deviceId)
        if (result != null) p.put("result", result)
        if (error != null) p.put("error", error)
        deliverJs(UUID.randomUUID().toString(), p.toString())
    }

    private fun calendarText(r: JSONObject): String {
        val evs = r.optJSONArray("events") ?: JSONArray()
        val date = r.optString("date")
        if (evs.length() == 0) return "今天（$date）日历上没有安排。"
        val hm = DateTimeFormatter.ofPattern("HH:mm").withZone(ZoneId.systemDefault())
        fun t(s: String) = try { hm.format(Instant.parse(s)) } catch (e: Exception) { "" }
        val lines = mutableListOf("今天（$date）有 ${evs.length()} 项安排：")
        for (i in 0 until evs.length()) {
            val e = evs.optJSONObject(i) ?: continue
            val whenText = if (e.optBoolean("all_day")) "全天" else "${t(e.optString("start"))}–${t(e.optString("end"))}"
            val loc = e.optString("location")
            lines.add("• $whenText ${e.optString("title")}" + if (loc.isEmpty()) "" else " @ $loc")
        }
        return lines.joinToString("\n")
    }
}
