package com.hackjown.aiw

import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/** First run and later edits: server URL (a pasted full web URL with ?token= works) + token, or a QR scan. */
class SetupActivity : AppCompatActivity() {
    private lateinit var url: EditText
    private lateinit var token: EditText
    private lateinit var notify: CheckBox
    private lateinit var status: TextView

    private val scan = registerForActivityResult(ScanContract()) { r ->
        val text = r?.contents ?: return@registerForActivityResult
        val p = Prefs.parse(text)
        if (p == null) { status.text = "二维码里不是网址：" + text.take(60); return@registerForActivityResult }
        url.setText(p.first)
        if (p.second.isNotEmpty()) token.setText(p.second)
        status.text = "已读取二维码"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_setup)
        url = findViewById(R.id.url); token = findViewById(R.id.token)
        notify = findViewById(R.id.notify); status = findViewById(R.id.status)
        url.setText(Prefs.server(this)); token.setText(Prefs.token(this)); notify.isChecked = Prefs.notifyEnabled(this)
        findViewById<TextView>(R.id.version).text = "Aiw Android ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})"
        findViewById<Button>(R.id.btn_scan).setOnClickListener {
            scan.launch(ScanOptions().setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                .setPrompt("扫描 Aiw 网页地址的二维码").setBeepEnabled(false).setOrientationLocked(false))
        }
        findViewById<Button>(R.id.btn_save).setOnClickListener { save() }
    }

    private fun save() {
        val p = Prefs.parse(url.text.toString())
        if (p == null) { status.text = "请填写服务器地址，例如 http://192.168.50.188:28000"; return }
        val server = p.first
        val tok = token.text.toString().trim().ifEmpty { p.second }
        url.setText(server); token.setText(tok)
        status.text = "正在连接…"
        thread {
            val err = check(server, tok)
            runOnUiThread {
                if (err == null) commit(server, tok)
                else AlertDialog.Builder(this).setTitle("连接失败").setMessage(err)
                    .setPositiveButton("仍然保存") { _, _ -> commit(server, tok) }
                    .setNegativeButton("取消") { _, _ -> status.text = err }.show()
            }
        }
    }

    private fun commit(server: String, tok: String) {
        Prefs.save(this, server, tok, notify.isChecked)
        AiwApp.schedulePoll(this, replace = true)
        startActivity(Intent(this, MainActivity::class.java).putExtra(MainActivity.EXTRA_RELOAD, true)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP))
        finish()
    }

    /** null when the server answers an authorised API call; else a readable reason. */
    private fun check(server: String, tok: String): String? = try {
        val h = URL("$server/api/notifications?unread=true").openConnection() as HttpURLConnection
        h.connectTimeout = 8000; h.readTimeout = 10000
        if (tok.isNotEmpty()) h.setRequestProperty("Authorization", "Bearer $tok")
        val code = h.responseCode; h.disconnect()
        when (code) { 200 -> null; 401, 403 -> "Token 不对（HTTP $code）"; else -> "服务器返回 HTTP $code" }
    } catch (e: Exception) { "连不上 $server：${e.javaClass.simpleName} ${e.message ?: ""}".trim() }
}
