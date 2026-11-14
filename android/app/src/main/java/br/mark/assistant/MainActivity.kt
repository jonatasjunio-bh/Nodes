package br.mark.assistant

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.NotificationManager
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.net.http.SslError
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.webkit.SslErrorHandler
import android.webkit.ValueCallback
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Switch
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import com.google.zxing.integration.android.IntentIntegrator
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLException

/**
 * Three screens, built in code (no layout files): pairing with the PC by QR
 * code, the permission checklist, and the assistant's dashboard in a WebView.
 */
class MainActivity : Activity() {

    private val bg = Color.parseColor("#07090f")
    private val fg = Color.parseColor("#dde3ed")
    private val accent = Color.parseColor("#00d4ff")
    private var web: WebView? = null
    private var pageOk = false                 // the dashboard really loaded
    private var loadStartedAt = 0L
    private val ui = Handler(Looper.getMainLooper())

    /** Every 5 s on the web screen: a dashboard that never loaded (the PC's
     *  page was down, restarting, or served the wrong certificate at that
     *  moment) is loaded again as soon as the link to the PC is up — before,
     *  one failed first load left the screen blank until the app was killed. */
    private val webWatch = object : Runnable {
        override fun run() {
            if (screen == "web" && !pageOk && MarkService.connected
                && (webFailed || SystemClock.elapsedRealtime() - loadStartedAt > 15_000)) {
                webFailed = false
                loadDashboard()
            }
            ui.postDelayed(this, 5_000)
        }
    }
    private var pendingWebPermission: PermissionRequest? = null
    private var screen = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Prefs.setRunning(this, true)            // opened: run again (it stays off once closed)
        Tailscale.ensure(this, force = true)
        if (Prefs.paired(this)) {
            MarkService.start(this)
            MarkMediaService.start(this)
            showWeb()
        } else {
            showPairing()
        }
    }

    override fun onResume() {
        super.onResume()
        if (!Prefs.running(this)) {             // closed earlier, brought back from Recents
            Prefs.setRunning(this, true)
            if (Prefs.paired(this)) MarkService.start(this)
        }
        if (Prefs.paired(this)) MarkMediaService.start(this)   // (re)gains the mic type on screen
        Tailscale.ensure(this)
        if (screen == "setup") showSetup()          // refresh after returning from Settings
        if (screen == "web" && !pageOk && web != null) { webFailed = false; loadDashboard() }
        ui.removeCallbacks(webWatch)
        ui.postDelayed(webWatch, 5_000)
    }

    override fun onPause() {
        ui.removeCallbacks(webWatch)
        super.onPause()
    }

    @Deprecated("Activity API")
    override fun onBackPressed() {
        val w = web
        when {
            screen == "web" && w != null && w.canGoBack() -> w.goBack()
            screen == "setup" && Prefs.paired(this) -> showWeb()
            else -> @Suppress("DEPRECATION") super.onBackPressed()
        }
    }

    // ── shared UI bits ───────────────────────────────────────────────────────

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun label(text: String, size: Float = 15f, bold: Boolean = false, color: Int = fg) =
        TextView(this).apply {
            this.text = text; textSize = size; setTextColor(color)
            if (bold) typeface = Typeface.DEFAULT_BOLD
            setPadding(0, dp(6), 0, dp(6))
        }

    private fun button(text: String, onClick: () -> Unit) = Button(this).apply {
        this.text = text; isAllCaps = false; setOnClickListener { onClick() }
    }

    private var statusView: TextView? = null
    @Volatile private var webFailed = false
    private val ticker = android.os.Handler(android.os.Looper.getMainLooper())

    /** The link state in the top bar, kept current every 2 s while it is shown. */
    private fun refreshStatus() {
        statusView?.apply {
            val on = MarkService.connected
            val route = VoiceLink.routeName?.let { "  🎧 $it" } ?: ""
            val vpn = Tailscale.vpnUp(this@MainActivity)
            text = when {
                on -> "  ● conectado ao PC$route"
                !vpn && Tailscale.installed(this@MainActivity) -> "  ○ ligando o Tailscale… (toque para abrir)"
                else -> "  ○ PC fora de alcance — o PC está ligado?"
            }
            setTextColor(if (on) Color.parseColor("#4ade80") else Color.parseColor("#f59e0b"))
            setOnClickListener { if (!MarkService.connected) Tailscale.open(this@MainActivity) }
            if (!on) Tailscale.ensure(this@MainActivity)
        }
        // The page failed while Tailscale was still coming up: once the PC
        // answers, load it again instead of leaving the error screen up.
        if (MarkService.connected && webFailed) {
            webFailed = false
            loadDashboard()
        }
        ticker.removeCallbacksAndMessages(null)
        if (screen == "web") ticker.postDelayed({ refreshStatus() }, 2000)
    }

    private fun column(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setBackgroundColor(bg)
        setPadding(dp(20), dp(24), dp(20), dp(24))
    }

    private fun topBar(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        setBackgroundColor(bg)
        setPadding(dp(14), dp(6), dp(6), dp(6))
        addView(label("MARK", 17f, true, accent))
        val status = label("", 12f)
        statusView = status
        refreshStatus()
        addView(status, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        addView(button("⚙") { showSetup() })
    }

    // ── pairing ──────────────────────────────────────────────────────────────

    private fun showPairing() {
        screen = "pair"
        val col = column()
        col.addView(label("MARK", 28f, true, accent))
        col.addView(label("Pareie com o assistente do seu PC:\n\n" +
                "1. No PC, abra ⚙ SETUP → REMOTE CONTROL.\n" +
                "2. Com o Tailscale ligado neste celular, escaneie o QR code."))
        col.addView(button("📷  Escanear QR code") { scanQr() })
        col.addView(label("\nOu digite o endereço do PC e uma chave — a do QR code ou a sua chave fixa:",
            13f, color = Color.GRAY))
        val url = EditText(this).apply { hint = "meu-pc.xxxx.ts.net"; setTextColor(fg); setHintTextColor(Color.GRAY) }
        val pin = EditText(this).apply {
            hint = "Chave"; setTextColor(fg); setHintTextColor(Color.GRAY)
            inputType = android.text.InputType.TYPE_CLASS_TEXT or
                    android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        col.addView(url); col.addView(pin)
        col.addView(button("Parear") { pair(url.text.toString().trim(), pin.text.toString().trim()) })
        setContentView(ScrollView(this).apply { setBackgroundColor(bg); addView(col) })
    }

    /** Google's scanner first (autofocus + auto-zoom); ZXing if Play services can't. */
    private fun scanQr() {
        val options = GmsBarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .enableAutoZoom()
            .build()
        GmsBarcodeScanning.getClient(this, options).startScan()
            .addOnSuccessListener { code -> handleQr(code.rawValue ?: "") }
            .addOnCanceledListener { }
            .addOnFailureListener {
                IntentIntegrator(this).setPrompt("Aponte para o QR code do MARK")
                    .setDesiredBarcodeFormats(IntentIntegrator.QR_CODE)
                    .setBeepEnabled(false).setOrientationLocked(true).initiateScan()
            }
    }

    /** The QR holds https://<pc>:8000/auto-login?key=XXXXXX */
    private fun handleQr(text: String) {
        val uri = Uri.parse(text)
        val key = uri.getQueryParameter("key") ?: ""
        val base = "${uri.scheme}://${uri.host}" + (if (uri.port > 0) ":${uri.port}" else "")
        if (key.isEmpty() || uri.host.isNullOrEmpty()) toast("Esse QR code não é do MARK.")
        else pair(base, key)
    }

    // The dashboard's 📎 button: the page asks for files, Android's picker answers.
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private val FILE_REQ = 4242

    // A photo taken with the camera from the 📎: where it is written, and its URI.
    private var cameraFile: java.io.File? = null
    private var cameraUri: Uri? = null
    private var pendingPick: Boolean? = null       // waiting for the camera permission

    private fun pickFiles(cb: ValueCallback<Array<Uri>>, multiple: Boolean): Boolean {
        fileCallback?.onReceiveValue(null)          // a picker left open: close its request
        fileCallback = cb
        // The camera needs Android's permission first (the app already asks for
        // it for QR pairing); the chooser opens once it is answered.
        if (!has(Manifest.permission.CAMERA)) {
            pendingPick = multiple
            requestPermissions(arrayOf(Manifest.permission.CAMERA), 11)
            return true
        }
        return openChooser(multiple)
    }

    private fun cameraIntent(): Intent? = try {
        val dir = java.io.File(cacheDir, "camera").apply { mkdirs() }
        dir.listFiles()?.forEach { if (System.currentTimeMillis() - it.lastModified() > 3_600_000) it.delete() }
        val f = java.io.File(dir, "foto_${System.currentTimeMillis()}.jpg")
        val uri = androidx.core.content.FileProvider.getUriForFile(this, "$packageName.files", f)
        cameraFile = f
        cameraUri = uri
        Intent(android.provider.MediaStore.ACTION_IMAGE_CAPTURE)
            .putExtra(android.provider.MediaStore.EXTRA_OUTPUT, uri)
            .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_READ_URI_PERMISSION)
            .apply { clipData = android.content.ClipData.newRawUri("foto", uri) }
            .takeIf { it.resolveActivity(packageManager) != null }
    } catch (_: Exception) { null }

    private fun openChooser(multiple: Boolean): Boolean {
        val pick = Intent(Intent.ACTION_GET_CONTENT)
            .addCategory(Intent.CATEGORY_OPENABLE)
            .setType("*/*")
            .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, multiple)
        val chooser = Intent.createChooser(pick, "Enviar para o MARK")
        cameraFile = null; cameraUri = null
        if (has(Manifest.permission.CAMERA)) {
            cameraIntent()?.let { chooser.putExtra(Intent.EXTRA_INITIAL_INTENTS, arrayOf(it)) }
        }
        return try {
            @Suppress("DEPRECATION")
            startActivityForResult(chooser, FILE_REQ)
            true
        } catch (e: Exception) {
            fileCallback?.onReceiveValue(null)
            fileCallback = null
            toast("Nenhum app para escolher arquivos.")
            false
        }
    }

    @Deprecated("Activity API")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == FILE_REQ) {
            val cb = fileCallback
            fileCallback = null
            val photo = cameraFile
            val photoUri = cameraUri
            cameraFile = null; cameraUri = null
            val tookPhoto = resultCode == Activity.RESULT_OK && photo != null && photo.length() > 0
            val uris: Array<Uri>? = when {
                resultCode != Activity.RESULT_OK -> null
                // the camera writes to our file and returns no data (or our own URI back)
                tookPhoto && (data?.data == null || data.data == photoUri) -> arrayOf(photoUri!!)
                data == null -> null
                else -> {
                    val clip = data.clipData
                    when {
                        clip != null && clip.itemCount > 0 && clip.getItemAt(0).uri != photoUri ->
                            Array(clip.itemCount) { clip.getItemAt(it).uri }
                        data.data != null -> arrayOf(data.data!!)
                        tookPhoto -> arrayOf(photoUri!!)
                        else -> null
                    }
                }
            }
            cb?.onReceiveValue(uris)
            return
        }
        val r = IntentIntegrator.parseActivityResult(requestCode, resultCode, data)
        if (r != null) {
            r.contents?.let { handleQr(it) }
            return
        }
        @Suppress("DEPRECATION") super.onActivityResult(requestCode, resultCode, data)
    }

    private fun pair(baseIn: String, key: String) {
        if (baseIn.isEmpty() || key.isEmpty()) { toast("Preencha o endereço e a chave."); return }
        var base = (if (baseIn.startsWith("http")) baseIn else "https://$baseIn").trimEnd('/')
        // A typed address without a port means the dashboard's default, 8000.
        if (Uri.parse(base).port <= 0) base += ":8000"
        toast("Pareando…")
        Thread {
            val msg = try {
                val body = JSONObject().put("pin", key.uppercase())
                    .put("device", "${Build.MANUFACTURER} ${Build.MODEL}").toString()
                val resp = OkHttpClient.Builder().callTimeout(20, TimeUnit.SECONDS).build()
                    .newCall(Request.Builder().url("$base/api/app-pair")
                        .post(body.toRequestBody("application/json".toMediaType())).build())
                    .execute()
                val j = JSONObject(resp.body?.string() ?: "{}")
                if (!j.optBoolean("ok")) j.optString("error", "o PC recusou a chave")
                else {
                    // Prefer the address the PC reports (its Tailscale name), so the
                    // link keeps working away from home.
                    val host = j.optString("host").ifEmpty { Uri.parse(base).host ?: "" }
                    val port = j.optInt("port", 8000)
                    Prefs.save(this, "https://$host:$port", j.getString("app_token"))
                    null
                }
            } catch (e: SSLException) {
                "o certificado do PC não é confiável — use o endereço do Tailscale (…ts.net), " +
                        "com o Tailscale ligado no PC e no celular"
            } catch (e: Exception) {
                "não consegui falar com o PC (${e.javaClass.simpleName}) — confira o Tailscale"
            }
            runOnUiThread {
                if (msg != null) toast("Falhou: $msg")
                else { toast("Pareado!"); MarkService.start(this); showSetup() }
            }
        }.start()
    }

    // ── permissions checklist ────────────────────────────────────────────────

    private fun has(p: String) = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    private fun notificationAccess(): Boolean {
        val enabled = Settings.Secure.getString(contentResolver, "enabled_notification_listeners") ?: ""
        return enabled.contains(ComponentName(this, MarkNotificationListener::class.java).flattenToString())
    }

    private fun showSetup() {
        screen = "setup"
        val col = column()
        col.addView(label("Permissões", 24f, true, accent))
        col.addView(label("Cada item libera um tipo de comando. Você pode deixar algum desligado " +
                "— o assistente avisa quando faltar permissão.", 13f, color = Color.GRAY))
        val nm = getSystemService(NotificationManager::class.java)
        val pm = getSystemService(PowerManager::class.java)

        fun row(title: String, desc: String, ok: Boolean, action: () -> Unit) {
            col.addView(label((if (ok) "✅  " else "⬜  ") + title, 16f, true))
            col.addView(label(desc, 13f, color = Color.GRAY))
            if (!ok) col.addView(button("Permitir") { action() })
        }

        row("Microfone", "Falar com o assistente pelo app.", has(Manifest.permission.RECORD_AUDIO)) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1)
        }
        if (Build.VERSION.SDK_INT >= 33) {
            row("Notificação do MARK", "Mostra que a conexão com o PC está ativa.",
                has(Manifest.permission.POST_NOTIFICATIONS)) {
                requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 2)
            }
        }
        row("Localização", "'Onde está meu celular?'", has(Manifest.permission.ACCESS_FINE_LOCATION)) {
            requestPermissions(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION), 3)
        }
        if (Build.VERSION.SDK_INT >= 29 && has(Manifest.permission.ACCESS_FINE_LOCATION)) {
            row("Localização em segundo plano", "Para responder com o app fechado — escolha \"Permitir o tempo todo\".",
                has(Manifest.permission.ACCESS_BACKGROUND_LOCATION)) {
                requestPermissions(arrayOf(Manifest.permission.ACCESS_BACKGROUND_LOCATION), 4)
            }
        }
        row("Contatos, ligações e SMS", "'Liga para a Ana', 'manda SMS para o João' — sempre com confirmação no PC.",
            has(Manifest.permission.READ_CONTACTS) && has(Manifest.permission.CALL_PHONE) &&
                    has(Manifest.permission.SEND_SMS)) {
            requestPermissions(arrayOf(Manifest.permission.READ_CONTACTS,
                Manifest.permission.CALL_PHONE, Manifest.permission.SEND_SMS), 5)
        }
        row("Acesso às notificações", "Avisar mensagens que chegam e responder por elas.", notificationAccess()) {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }
        row("Não perturbe", "Colocar o celular no silencioso.", nm.isNotificationPolicyAccessGranted) {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS))
        }
        row("Sobrepor a outros apps", "Abrir apps, alarmes, mapas e ligações com o app em segundo plano.",
            Settings.canDrawOverlays(this)) {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
        }
        row("Sem economia de bateria", "Evita que o Android desligue a conexão com o PC.",
            pm.isIgnoringBatteryOptimizations(packageName)) {
            @SuppressLint("BatteryLife")
            val i = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))
            startActivity(i)
        }
        col.addView(label("\nVoz", 16f, true, accent))
        col.addView(Switch(this).apply {
            text = "Respostas por voz no celular"
            setTextColor(fg); textSize = 15f
            isChecked = Prefs.speakerOn(this@MainActivity)
            setOnCheckedChangeListener { _, on -> Prefs.setSpeakerOn(this@MainActivity, on); VoiceLink.setSpeaker(on) }
        })
        col.addView(label("Com um fone Bluetooth, o som do carro ou o Android Auto conectados, " +
                "toda resposta do assistente sai por eles — mesmo com a tela apagada.", 13f, color = Color.GRAY))
        col.addView(Switch(this).apply {
            text = "Usar o microfone do Bluetooth"
            setTextColor(fg); textSize = 15f
            isChecked = Prefs.btMic(this@MainActivity)
            setOnCheckedChangeListener { _, on -> Prefs.setBtMic(this@MainActivity, on) }
        })
        col.addView(label("Ligado, o carro / fone entra em modo de ligação: o microfone dele funciona, " +
                "mas a voz do assistente fica mono e com qualidade de telefone (em muitos carros só num " +
                "lado). Desligado (recomendado), a voz sai em estéreo pelo som do carro e você fala pelo " +
                "microfone do celular.", 13f, color = Color.GRAY))
        if (Build.MANUFACTURER.equals("Xiaomi", true) || Build.MANUFACTURER.equals("Redmi", true) ||
            Build.MANUFACTURER.equals("POCO", true)) {
            col.addView(label("\nXiaomi / Redmi", 16f, true, accent))
            col.addView(label("Nas informações do app, ative também \"Início automático\" e, em " +
                    "Outras permissões, \"Exibir janelas pop-up em segundo plano\". Sem isso o HyperOS " +
                    "desliga o MARK e bloqueia abrir telas.", 13f, color = Color.GRAY))
            col.addView(button("Abrir informações do app") {
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
            })
        }
        col.addView(View(this), LinearLayout.LayoutParams(MATCH_PARENT, dp(16)))
        col.addView(button("Concluir") { showWeb() })
        col.addView(button("Desparear este celular") {
            stopService(Intent(this, MarkService::class.java))
            Prefs.clear(this)
            showPairing()
        })
        setContentView(ScrollView(this).apply { setBackgroundColor(bg); addView(col) })
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 11) {                   // camera, asked by the 📎: open the chooser now
            pendingPick?.let { m -> pendingPick = null; openChooser(m) }
            return
        }
        if (has(Manifest.permission.RECORD_AUDIO)) MarkMediaService.instance?.goForeground()
        pendingWebPermission?.let { req ->
            if (has(Manifest.permission.RECORD_AUDIO)) req.grant(req.resources) else req.deny()
            pendingWebPermission = null
        }
        if (screen == "setup") showSetup()
    }

    // ── the dashboard ────────────────────────────────────────────────────────

    @Volatile private var pageUrl = ""

    /**
     * window.MarkNative in the dashboard page: the mic button and the speaker
     * toggle drive VoiceLink, so the voice goes through Bluetooth / the car and
     * keeps going with the screen off. Only the PC's own pages may use it.
     */
    inner class NativeBridge {
        private fun ours() = pageUrl.startsWith(Prefs.baseUrl(this@MainActivity))

        @JavascriptInterface fun mic(on: Boolean) {
            if (!ours()) return
            runOnUiThread {
                if (on && !has(Manifest.permission.RECORD_AUDIO)) {
                    requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 11)
                } else {
                    MarkMediaService.start(this@MainActivity)
                    VoiceLink.setListening(on)
                }
            }
        }
        @JavascriptInterface fun listening(): Boolean = VoiceLink.listening
        @JavascriptInterface fun speaker(on: Boolean) { if (ours()) VoiceLink.setSpeaker(on) }
        @JavascriptInterface fun speakerOn(): Boolean = Prefs.speakerOn(this@MainActivity)
        @JavascriptInterface fun route(): String = VoiceLink.routeName ?: ""

        /** A link on a HUD card (a store, a product page…): opens in the phone's
         *  browser, so the assistant's screen stays where it was. Web links only. */
        @JavascriptInterface fun openUrl(url: String) {
            if (!ours()) return
            runOnUiThread { openExternal(url) }
        }
    }

    private fun openExternal(url: String): Boolean {
        val uri = Uri.parse(url)
        if (uri.scheme != "http" && uri.scheme != "https") return false
        return try {
            startActivity(Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            true
        } catch (e: Exception) {
            Toast.makeText(this, "Nenhum navegador para abrir o link", Toast.LENGTH_SHORT).show()
            false
        }
    }

    @SuppressLint("SetJavaScriptEnabled", "JavascriptInterface")
    private fun showWeb() {
        if (!Prefs.paired(this)) { showPairing(); return }
        screen = "web"
        val w = web ?: WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mediaPlaybackRequiresUserGesture = false      // the assistant's voice plays here
            setBackgroundColor(bg)
            addJavascriptInterface(NativeBridge(), "MarkNative")
            webViewClient = object : WebViewClient() {
                override fun onPageStarted(view: WebView, url: String?, favicon: android.graphics.Bitmap?) {
                    pageUrl = url ?: ""
                }

                override fun onPageFinished(view: WebView, url: String?) {
                    val u = url ?: ""
                    if (!webFailed && u.startsWith(Prefs.baseUrl(this@MainActivity)) && !u.contains("/app-login")) {
                        pageOk = true
                    }
                }

                // A certificate the app doesn't trust (the PC's dashboard opened
                // before its Tailscale certificate was in place): say so and retry,
                // instead of the WebView's silent blank page.
                override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                    handler.cancel()
                    webFailed = true
                    pageOk = false
                    view.loadDataWithBaseURL(null, """
                        <html><body style="background:#07090f;color:#dde3ed;font-family:sans-serif;
                        padding:32px;text-align:center">
                        <h2 style="color:#00d4ff">Reconectando ao painel…</h2>
                        <p>O certificado do PC ainda não confere (o assistente pode ter aberto antes
                        do Tailscale). O app tenta de novo sozinho em alguns segundos.</p>
                        <button onclick="location.href='mark://retry'" style="margin-top:20px;padding:14px 28px;
                        font-size:16px;border-radius:10px;border:0;background:#00d4ff;color:#07090f">
                        Tentar agora</button></body></html>""".trimIndent(), "text/html", "utf-8", null)
                }

                // Instead of Chrome's bare "no internet" page: say what is wrong
                // (almost always Tailscale switched off on the phone) and offer a retry.
                override fun onReceivedError(view: WebView, request: android.webkit.WebResourceRequest,
                                             error: android.webkit.WebResourceError) {
                    if (!request.isForMainFrame) return
                    webFailed = true
                    pageOk = false
                    view.loadDataWithBaseURL(null, """
                        <html><body style="background:#07090f;color:#dde3ed;font-family:sans-serif;
                        padding:32px;text-align:center">
                        <h2 style="color:#00d4ff">PC fora de alcance</h2>
                        <p>O MARK não conseguiu falar com o seu PC.</p>
                        <p style="color:#8a94a6">Confira se o <b>Tailscale</b> está ligado neste celular
                        e se o PC está ligado com o assistente aberto.</p>
                        <p style="color:#5e6a7e;font-size:12px">${error.description}</p>
                        <button onclick="location.href='mark://retry'" style="margin-top:20px;padding:14px 28px;
                        font-size:16px;border-radius:10px;border:0;background:#00d4ff;color:#07090f">
                        Tentar de novo</button></body></html>""".trimIndent(), "text/html", "utf-8", null)
                }

                override fun shouldOverrideUrlLoading(view: WebView,
                                                      request: android.webkit.WebResourceRequest): Boolean {
                    if (request.url.scheme == "mark") {           // the retry button
                        Tailscale.ensure(this@MainActivity, force = true)
                        webFailed = false
                        loadDashboard()
                        return true
                    }
                    // Anything outside the assistant's own pages (a link on a HUD
                    // card) opens in the browser instead of replacing the dashboard.
                    val base = Uri.parse(Prefs.baseUrl(this@MainActivity))
                    if (request.isForMainFrame && request.url.host != base.host) {
                        return openExternal(request.url.toString())
                    }
                    return false
                }
            }
            webChromeClient = object : WebChromeClient() {
                override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>,
                                               params: FileChooserParams): Boolean =
                    pickFiles(callback, params.mode == FileChooserParams.MODE_OPEN_MULTIPLE)

                override fun onPermissionRequest(request: PermissionRequest) {
                    // The dashboard's mic button: grant once Android allowed the app itself.
                    runOnUiThread {
                        if (has(Manifest.permission.RECORD_AUDIO)) request.grant(request.resources)
                        else {
                            pendingWebPermission = request
                            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 10)
                        }
                    }
                }
            }
        }
        web = w
        if (w.url == null) loadDashboard()
        (w.parent as? LinearLayout)?.removeView(w)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(bg)
            addView(topBar())
            addView(w, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        }
        setContentView(root)
    }

    /** POST the device token so it never sits in a URL or the history. */
    private fun loadDashboard() {
        pageOk = false
        loadStartedAt = SystemClock.elapsedRealtime()
        web?.postUrl("${Prefs.baseUrl(this)}/app-login",
            ("app_token=" + URLEncoder.encode(Prefs.token(this), "UTF-8")).toByteArray())
    }

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_LONG).show()
}
