package com.hackjown.aiw

import android.Manifest
import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.ActivityNotFoundException
import android.content.ContentValues
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.util.Base64
import android.view.View
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.MimeTypeMap
import android.webkit.PermissionRequest
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import androidx.webkit.WebSettingsCompat
import androidx.webkit.WebViewFeature
import java.io.File
import java.io.FileOutputStream

/** A thin shell around the Aiw web console (apps/web), signed in with the saved server + token. */
class MainActivity : AppCompatActivity() {
    companion object {
        const val EXTRA_OPEN = "open"       // deep path: task/<id>, chat, monitor/<id> …
        const val EXTRA_RELOAD = "reload"   // settings changed: load the (new) server again
    }

    private lateinit var web: WebView
    private lateinit var swipe: SwipeRefreshLayout
    private lateinit var errorView: View
    @Volatile private var pageAtTop = true            // set by the page: is the touched scroll area at its top?
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var fileParams: WebChromeClient.FileChooserParams? = null
    private var captureUri: Uri? = null
    private var pendingWebPermission: PermissionRequest? = null
    private var pendingDownload: (() -> Unit)? = null
    private var loadedServer = ""
    // v3.9.28 手机快通道: on-device answers; results go back to the page (home.js window.aiwFastResult)
    private val fast by lazy {
        FastPath(this) { cid, payload ->
            runOnUiThread { if (::web.isInitialized) web.evaluateJavascript("window.aiwFastResult&&window.aiwFastResult(${jsStr(cid)},${jsStr(payload)})", null) }
        }
    }

    // ---------------------------------------------------------------- activity results
    private val pickFiles = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { r ->
        val cb = fileCallback ?: return@registerForActivityResult
        fileCallback = null
        var out: Array<Uri>? = null
        if (r.resultCode == RESULT_OK) {
            val d = r.data
            val clip = d?.clipData
            out = when {
                clip != null && clip.itemCount > 0 -> Array(clip.itemCount) { clip.getItemAt(it).uri }
                d?.data != null -> arrayOf(d.data!!)
                captureUri != null && captured() -> arrayOf(captureUri!!)
                else -> WebChromeClient.FileChooserParams.parseResult(r.resultCode, d)
            }
        }
        captureUri = null
        cb.onReceiveValue(out)
    }

    private val cameraPerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) { openChooser() }

    private val audioPerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        val req = pendingWebPermission ?: return@registerForActivityResult
        pendingWebPermission = null
        if (ok) req.grant(req.resources) else req.deny()
    }

    private val storagePerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        val run = pendingDownload; pendingDownload = null
        if (ok) run?.invoke() else toast("没有存储权限，无法下载")
    }

    private val notifPerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    // ---------------------------------------------------------------- lifecycle
    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!Prefs.configured(this)) {
            startActivity(Intent(this, SetupActivity::class.java)); finish(); return
        }
        setContentView(R.layout.activity_main)
        web = findViewById(R.id.web)
        swipe = findViewById(R.id.swipe)
        errorView = findViewById(R.id.error)
        findViewById<Button>(R.id.btn_retry).setOnClickListener { errorView.visibility = View.GONE; web.reload() }
        findViewById<Button>(R.id.btn_settings).setOnClickListener { openSettings() }

        setupWeb()
        swipe.setColorSchemeColors(ContextCompat.getColor(this, R.color.accent))
        swipe.setOnRefreshListener { web.reload() }
        // only pull-to-refresh when the page (and the inner scroll area under the finger) is at its top
        swipe.setOnChildScrollUpCallback { _, _ -> web.scrollY > 0 || !pageAtTop }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (errorView.visibility == View.VISIBLE) { errorView.visibility = View.GONE }
                if (web.canGoBack()) web.goBack() else askExit()
            }
        })

        if (Build.VERSION.SDK_INT >= 33 && Prefs.notifyEnabled(this) &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)
        }

        if (savedInstanceState != null) web.restoreState(savedInstanceState)
        if (deepOf(intent) == "open") DeviceBridge.get(this).connectNow()   // v3.9.28 wake push
        if (web.url == null) { loadedServer = Prefs.server(this); web.loadUrl(Prefs.webUrl(this, deepOf(intent)?.takeIf { it != "open" })) }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (!::web.isInitialized) return
        if (intent.getBooleanExtra(EXTRA_RELOAD, false) || loadedServer != Prefs.server(this)) {
            loadedServer = Prefs.server(this)
            web.clearHistory(); web.loadUrl(Prefs.webUrl(this, deepOf(intent))); return
        }
        val open = deepOf(intent) ?: return
        if (open == "open") { DeviceBridge.get(this).connectNow(); return }   // v3.9.28: wake push for the phone fast path
        // route inside the running page (deeplink.js window.aiwOpen); load ?open= when the page has no router
        web.evaluateJavascript("(function(p){if(typeof window.aiwOpen==='function'){window.aiwOpen(p);return 1}return 0})(${jsStr(open)})") {
            if (it != "1") web.loadUrl(Prefs.webUrl(this, open))
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        if (::web.isInitialized) web.saveState(outState)
    }

    override fun onResume() { super.onResume(); if (::web.isInitialized) web.onResume(); DeviceBridge.get(this).connectNow() }   // v3.9.27; v3.9.28 at once
    override fun onPause() { if (::web.isInitialized) web.onPause(); CookieManager.getInstance().flush(); super.onPause() }
    override fun onStop() { DeviceBridge.get(this).stop(); super.onStop() }   // v3.9.27: calls queue on the server while away

    /** EXTRA_OPEN from a notification, or aiw://task/<id> from a Bark / ntfy push link. */
    private fun deepOf(i: Intent?): String? {
        i ?: return null
        i.getStringExtra(EXTRA_OPEN)?.let { return it }
        val d = i.data ?: return null
        if (d.scheme == "aiw") return d.toString().removePrefix("aiw://").trim('/').ifEmpty { null }
        return null
    }

    // ---------------------------------------------------------------- WebView
    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWeb() {
        val s = web.settings
        s.javaScriptEnabled = true
        s.domStorageEnabled = true
        s.databaseEnabled = true
        s.mediaPlaybackRequiresUserGesture = false
        s.allowFileAccess = false
        s.allowContentAccess = true
        s.loadWithOverviewMode = true
        s.useWideViewPort = true
        s.setSupportZoom(false)
        s.mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
        s.cacheMode = WebSettings.LOAD_DEFAULT
        s.javaScriptCanOpenWindowsAutomatically = true
        s.userAgentString = s.userAgentString + " AiwAndroid/" + BuildConfig.VERSION_NAME
        // dark mode: the page's own prefers-color-scheme styles win; pages without them are darkened by WebView
        if (WebViewFeature.isFeatureSupported(WebViewFeature.ALGORITHMIC_DARKENING)) {
            WebSettingsCompat.setAlgorithmicDarkeningAllowed(s, true)
        }
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true)
        web.setBackgroundColor(ContextCompat.getColor(this, R.color.bg))
        web.addJavascriptInterface(Bridge(), "AiwAndroid")

        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, req: WebResourceRequest): Boolean {
                val u = req.url
                return when (u.scheme?.lowercase()) {
                    "aiw" -> { val p = u.toString().removePrefix("aiw://").trim('/'); if (p == "open") DeviceBridge.get(this@MainActivity).connectNow() else routeDeep(p); true }
                    "http", "https" -> if (Prefs.sameServer(this@MainActivity, u.toString())) false else { external(u); true }
                    "blob", "data", "about", "javascript" -> false
                    else -> { external(u); true }   // tel:, mailto:, intent: …
                }
            }

            override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) { pageAtTop = true }

            override fun onPageFinished(view: WebView, url: String?) {
                swipe.isRefreshing = false
                view.evaluateJavascript(PAGE_JS, null)
            }

            override fun onReceivedError(view: WebView, req: WebResourceRequest, err: WebResourceError) {
                if (!req.isForMainFrame) return
                swipe.isRefreshing = false
                findViewById<TextView>(R.id.error_detail).text = "${Prefs.server(this@MainActivity)}\n${err.description}"
                errorView.visibility = View.VISIBLE
            }
        }

        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(view: WebView, cb: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = cb; fileParams = params
                if (ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                    cameraPerm.launch(Manifest.permission.CAMERA)   // chooser opens either way; camera only if granted
                } else openChooser()
                return true
            }

            override fun onPermissionRequest(request: PermissionRequest) {
                runOnUiThread {
                    val wantsMic = request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)
                    val wantsCam = request.resources.contains(PermissionRequest.RESOURCE_VIDEO_CAPTURE)
                    val perm = if (wantsMic) Manifest.permission.RECORD_AUDIO else if (wantsCam) Manifest.permission.CAMERA else null
                    if (perm == null) { request.deny(); return@runOnUiThread }
                    if (ContextCompat.checkSelfPermission(this@MainActivity, perm) == PackageManager.PERMISSION_GRANTED) request.grant(request.resources)
                    else { pendingWebPermission = request; audioPerm.launch(perm) }
                }
            }

            override fun onCreateWindow(view: WebView, isDialog: Boolean, isUserGesture: Boolean, resultMsg: android.os.Message): Boolean {
                // target=_blank / window.open: open same-server pages here, others in the browser
                val tmp = WebView(this@MainActivity)
                tmp.webViewClient = object : WebViewClient() {
                    override fun shouldOverrideUrlLoading(v: WebView, req: WebResourceRequest): Boolean {
                        val u = req.url
                        if (Prefs.sameServer(this@MainActivity, u.toString())) web.loadUrl(u.toString()) else external(u)
                        v.destroy(); return true
                    }
                }
                (resultMsg.obj as WebView.WebViewTransport).webView = tmp
                resultMsg.sendToTarget()
                return true
            }
        }

        web.setDownloadListener { url, ua, contentDisposition, mime, _ ->
            when {
                url.startsWith("blob:") -> web.evaluateJavascript(blobJs(url, URLUtil.guessFileName(url, contentDisposition, mime), mime), null)
                url.startsWith("data:") -> saveDataUrl(url, URLUtil.guessFileName(url, contentDisposition, mime))
                else -> download(url, ua, contentDisposition, mime)
            }
        }
    }

    private fun routeDeep(path: String) {
        val p = path.trim('/')
        web.evaluateJavascript("(function(p){if(typeof window.aiwOpen==='function'){window.aiwOpen(p);return 1}return 0})(${jsStr(p)})") {
            if (it != "1") web.loadUrl(Prefs.webUrl(this, p))
        }
    }

    private fun external(u: Uri) {
        try { startActivity(Intent(Intent.ACTION_VIEW, u).addCategory(Intent.CATEGORY_BROWSABLE)) }
        catch (_: ActivityNotFoundException) { toast("没有能打开这个链接的应用") }
    }

    private fun askExit() {
        AlertDialog.Builder(this).setTitle("退出 Aiw？")
            .setPositiveButton("退出") { _, _ -> finish() }
            .setNeutralButton(R.string.setup_title) { _, _ -> openSettings() }
            .setNegativeButton("取消", null).show()
    }

    private fun openSettings() { startActivity(Intent(this, SetupActivity::class.java)) }

    // ---------------------------------------------------------------- uploads (camera, photos/videos, files)
    private fun openChooser() {
        val params = fileParams
        val accept = params?.acceptTypes?.flatMap { it.split(',') }?.map { it.trim().lowercase() }?.filter { it.isNotEmpty() } ?: emptyList()
        val mimes = accept.mapNotNull { a ->
            if (a.startsWith(".")) MimeTypeMap.getSingleton().getMimeTypeFromExtension(a.drop(1)) else a
        }.distinct()
        val wantImg = mimes.isEmpty() || mimes.any { it.startsWith("image/") || it == "*/*" }
        val wantVid = mimes.isEmpty() || mimes.any { it.startsWith("video/") || it == "*/*" }

        val content = Intent(Intent.ACTION_GET_CONTENT).addCategory(Intent.CATEGORY_OPENABLE)
        when {
            mimes.isEmpty() -> content.type = "*/*"
            mimes.size == 1 -> content.type = mimes[0]
            else -> { content.type = "*/*"; content.putExtra(Intent.EXTRA_MIME_TYPES, mimes.toTypedArray()) }
        }
        if (params?.mode == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE) content.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)

        val extra = mutableListOf<Intent>()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            captureUri = null
            if (wantImg) newCapture("jpg")?.let { u ->
                captureUri = u
                extra.add(Intent(MediaStore.ACTION_IMAGE_CAPTURE).putExtra(MediaStore.EXTRA_OUTPUT, u)
                    .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_READ_URI_PERMISSION))
            }
            if (wantVid) extra.add(Intent(MediaStore.ACTION_VIDEO_CAPTURE))   // the camera app returns its own content uri
        }
        val chooser = Intent.createChooser(content, "选择照片、视频或文件")
        if (extra.isNotEmpty()) chooser.putExtra(Intent.EXTRA_INITIAL_INTENTS, extra.toTypedArray())
        try { pickFiles.launch(chooser) } catch (e: ActivityNotFoundException) {
            fileCallback?.onReceiveValue(null); fileCallback = null; toast("没有可选文件的应用")
        }
    }

    private fun newCapture(ext: String): Uri? = try {
        val dir = File(cacheDir, "capture").apply { mkdirs() }
        dir.listFiles()?.filter { System.currentTimeMillis() - it.lastModified() > 24 * 3600_000L }?.forEach { it.delete() }
        val f = File(dir, "aiw_${System.currentTimeMillis()}.$ext")
        FileProvider.getUriForFile(this, "$packageName.files", f)
    } catch (e: Exception) { null }

    private fun captured(): Boolean = try {
        contentResolver.openFileDescriptor(captureUri!!, "r")?.use { it.statSize > 0 } ?: false
    } catch (e: Exception) { false }

    // ---------------------------------------------------------------- downloads
    private fun download(url: String, ua: String?, contentDisposition: String?, mime: String?) {
        val run = {
            try {
                val name = URLUtil.guessFileName(url, contentDisposition, mime)
                val req = DownloadManager.Request(Uri.parse(url))
                    .setTitle(name)
                    .setMimeType(mime)
                    .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                    .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
                CookieManager.getInstance().getCookie(url)?.let { req.addRequestHeader("Cookie", it) }
                if (ua != null) req.addRequestHeader("User-Agent", ua)
                if (Prefs.sameServer(this, url) && Prefs.token(this).isNotEmpty()) req.addRequestHeader("Authorization", "Bearer " + Prefs.token(this))
                getSystemService(DownloadManager::class.java).enqueue(req)
                toast("开始下载：$name")
            } catch (e: Exception) { toast("下载失败：${e.message}") }
        }
        if (Build.VERSION.SDK_INT < 29 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
            pendingDownload = run; storagePerm.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)
        } else run()
    }

    private fun saveDataUrl(url: String, name: String) {
        val comma = url.indexOf(',')
        if (comma < 0) return
        val head = url.substring(5, comma)
        val mime = head.substringBefore(';').ifEmpty { "application/octet-stream" }
        val bytes = if (head.contains(";base64")) Base64.decode(url.substring(comma + 1), Base64.DEFAULT)
                    else Uri.decode(url.substring(comma + 1)).toByteArray()
        saveBytes(name, mime, bytes)
    }

    private fun saveBytes(name: String, mime: String, bytes: ByteArray) {
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                val v = ContentValues().apply {
                    put(MediaStore.Downloads.DISPLAY_NAME, name); put(MediaStore.Downloads.MIME_TYPE, mime)
                    put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS)
                }
                val u = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v) ?: throw IllegalStateException("MediaStore")
                contentResolver.openOutputStream(u)?.use { it.write(bytes) }
            } else {
                val dir = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: filesDir
                FileOutputStream(File(dir, name)).use { it.write(bytes) }
            }
            toast("已保存到下载：$name")
        } catch (e: Exception) { toast("保存失败：${e.message}") }
    }

    private fun blobJs(url: String, name: String, mime: String?) = """
        (function(){fetch(${jsStr(url)}).then(function(r){return r.blob()}).then(function(b){
          var fr=new FileReader();fr.onloadend=function(){var s=String(fr.result);
            AiwAndroid.saveBase64(${jsStr(name)}, b.type||${jsStr(mime ?: "application/octet-stream")}, s.substring(s.indexOf(',')+1));};
          fr.readAsDataURL(b);}).catch(function(e){AiwAndroid.toast('下载失败：'+e)});})();
    """.trimIndent()

    private fun jsStr(s: String): String = org.json.JSONObject.quote(s)
    private fun toast(s: String) = runOnUiThread { Toast.makeText(this, s, Toast.LENGTH_SHORT).show() }

    /** window.AiwAndroid in the page. */
    inner class Bridge {
        @JavascriptInterface fun setAtTop(top: Boolean) { pageAtTop = top }
        @JavascriptInterface fun openSettings() { runOnUiThread { this@MainActivity.openSettings() } }
        @JavascriptInterface fun toast(s: String) { this@MainActivity.toast(s) }
        @JavascriptInterface fun version(): String = BuildConfig.VERSION_NAME
        /** v3.9.28 手机快通道: true = the phone answers this message itself (FastPath.kt), the page must not send it */
        @JavascriptInterface fun fastTry(text: String): Boolean = try { fast.tryHandle(text) } catch (e: Exception) { false }
        @JavascriptInterface fun saveBase64(name: String, mime: String, b64: String) {
            try { saveBytes(name.ifEmpty { "download" }, mime, Base64.decode(b64, Base64.DEFAULT)) } catch (e: Exception) { toast("保存失败") }
        }
    }
}

/** Injected after each page load: tells the app whether the scroll area under the finger is at its top, so
 *  pull-to-refresh does not fire while scrolling up inside a chat list or a sheet. */
private const val PAGE_JS = """
(function(){ if (window.__aiwAndroid) return; window.__aiwAndroid = 1;
  function topOf(el){ for (var e = el; e && e !== document.documentElement; e = e.parentElement){
      if (e.scrollTop > 0) return false;
      var cs = getComputedStyle(e); if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && e.scrollHeight > e.clientHeight && e.scrollTop > 0) return false; }
    return (document.scrollingElement || document.documentElement).scrollTop <= 0; }
  document.addEventListener('touchstart', function(ev){ try { AiwAndroid.setAtTop(topOf(ev.target)); } catch(_){} }, {passive:true, capture:true});
  document.addEventListener('scroll', function(ev){ try { var t = ev.target && ev.target.nodeType === 1 ? ev.target : document.scrollingElement; if (t && t.scrollTop > 0) AiwAndroid.setAtTop(false); } catch(_){} }, {passive:true, capture:true});
})();
"""
