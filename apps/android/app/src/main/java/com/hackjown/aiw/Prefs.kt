package com.hackjown.aiw

import android.content.Context
import android.net.Uri

/** Server address + token, kept in app-private SharedPreferences (never logged). */
object Prefs {
    private const val FILE = "aiw"
    private fun sp(c: Context) = c.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun server(c: Context): String = sp(c).getString("server", "") ?: ""
    fun token(c: Context): String = sp(c).getString("token", "") ?: ""
    fun notifyEnabled(c: Context): Boolean = sp(c).getBoolean("notify", true)
    fun configured(c: Context) = server(c).isNotEmpty()

    fun save(c: Context, server: String, token: String, notify: Boolean) {
        sp(c).edit().putString("server", server).putString("token", token).putBoolean("notify", notify)
            .remove("last_seen").apply()
    }

    /** created_at (epoch seconds) of the newest notification already handled by the poller; -1 = never polled. */
    fun lastSeen(c: Context): Double = sp(c).getString("last_seen", null)?.toDoubleOrNull() ?: -1.0
    fun setLastSeen(c: Context, v: Double) { sp(c).edit().putString("last_seen", v.toString()).apply() }

    /**
     * Accepts what the user pastes or scans:
     *   http://192.168.50.188:28000/?token=abc   → (http://192.168.50.188:28000, abc)
     *   192.168.50.188:28000                     → (http://192.168.50.188:28000, "")
     *   https://aiw.example.com/some/            → (https://aiw.example.com/some, "")
     * Returns null when nothing usable is there.
     */
    fun parse(input: String): Pair<String, String>? {
        var s = input.trim()
        if (s.isEmpty()) return null
        if (!s.contains("://")) s = "http://$s"
        val u = Uri.parse(s)
        val scheme = u.scheme?.lowercase() ?: return null
        if (scheme != "http" && scheme != "https") return null
        val host = u.host ?: return null
        if (host.isEmpty()) return null
        val port = if (u.port > 0) ":${u.port}" else ""
        var path = u.path ?: ""
        if (path.endsWith("/index.html")) path = path.removeSuffix("index.html")
        if (path.startsWith("/open/")) path = ""
        path = path.trimEnd('/')
        val tok = try { u.getQueryParameter("token") ?: "" } catch (e: Exception) { "" }
        return Pair("$scheme://$host$port$path", tok)
    }

    /** The web console, signed in with ?token=, optionally routed to a deep-link path (task/<id>, chat …). */
    fun webUrl(c: Context, open: String? = null): String {
        val b = Uri.parse(server(c) + "/").buildUpon()
        val t = token(c)
        if (t.isNotEmpty()) b.appendQueryParameter("token", t)
        if (!open.isNullOrEmpty()) b.appendQueryParameter("open", open)
        return b.build().toString()
    }

    fun sameServer(c: Context, url: String): Boolean {
        val a = Uri.parse(server(c)); val b = Uri.parse(url)
        return a.host != null && a.host.equals(b.host, ignoreCase = true) && a.port == b.port
    }
}
