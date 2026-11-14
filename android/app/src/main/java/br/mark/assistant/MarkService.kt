package br.mark.assistant

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Keeps the link to the PC open (wss://<pc>/ws/device) while the phone is on,
 * runs the assistant's commands and reports notifications back. A foreground
 * service, so Android doesn't kill it with the screen off.
 */
class MarkService : Service() {

    companion object {
        const val CHANNEL = "mark_link"
        @Volatile var instance: MarkService? = null
        @Volatile var connected = false

        fun start(c: Context) {
            if (!Prefs.paired(c) || !Prefs.running(c)) return
            val i = Intent(c, MarkService::class.java)
            if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i) else c.startService(i)
        }

        /** The user closed MARK: everything stops and stays stopped (Tailscale
         *  is no longer woken up either) until the app is opened again. */
        fun shutdown(c: Context) {
            Prefs.setRunning(c, false)
            instance?.let { it.stopForeground(Service.STOP_FOREGROUND_REMOVE); it.stopSelf() }
            MarkMediaService.instance?.let { it.stopForeground(Service.STOP_FOREGROUND_REMOVE); it.stopSelf() }
            try { c.stopService(Intent(c, MarkService::class.java)) } catch (_: Exception) {}
            try { c.stopService(Intent(c, MarkMediaService::class.java)) } catch (_: Exception) {}
        }
    }

    private val main = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private val client = OkHttpClient.Builder()
        .pingInterval(25, TimeUnit.SECONDS)        // keeps NAT/Tailscale paths alive
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()
    private var ws: WebSocket? = null
    private var backoffMs = 2000L
    private var failures = 0                 // in a row, since the last good connection
    private var lastText: String? = null     // what the notification says now
    private var stopping = false
    private lateinit var handler: CommandHandler

    private val netCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            // Network came back (Wi-Fi ↔ 4G): reconnect now instead of waiting.
            main.post {
                if (!connected) {
                    backoffMs = 2000L
                    failures = 0
                    Tailscale.ensure(this@MarkService, force = true)   // e.g. just left home's Wi-Fi
                    scheduleReconnect(300)
                }
            }
            VoiceLink.kick()
        }
    }

    override fun onCreate() {
        super.onCreate()
        instance = this
        handler = CommandHandler(this)
        createChannel()
        val n = buildNotification("Conectando ao PC…")
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(1, n)
        }
        if (!Prefs.running(this)) { stopping = true; stopSelf(); return }
        (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager)
            .registerDefaultNetworkCallback(netCallback)
        Tailscale.ensure(this, force = true)
        connect()
        MarkMediaService.start(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Android restarting a sticky service after the user closed the app: stay closed.
        if (!Prefs.running(this)) { stopForeground(STOP_FOREGROUND_REMOVE); stopSelf(); return START_NOT_STICKY }
        return START_STICKY
    }

    // Swiped out of Recents = closed.
    override fun onTaskRemoved(rootIntent: Intent?) {
        shutdown(this)
        super.onTaskRemoved(rootIntent)
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        stopping = true
        instance = null
        connected = false
        try {
            (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager).unregisterNetworkCallback(netCallback)
        } catch (_: Exception) {}
        ws?.close(1000, "bye")
        handler.release()
        worker.shutdown()
        super.onDestroy()
    }

    private fun connect() {
        if (stopping || !Prefs.paired(this)) return
        main.removeCallbacks(reconnect)
        val old = ws
        ws = null                       // its cancellation must not count as a loss
        old?.cancel()
        val base = Prefs.baseUrl(this).replaceFirst("https://", "wss://")
        val req = Request.Builder()
            .url("$base/ws/device")
            .header("Authorization", "Bearer ${Prefs.token(this)}")
            .build()
        ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                connected = true
                backoffMs = 2000L
                failures = 0
                updateNotification("Conectado ao PC")
                webSocket.send(JSONObject()
                    .put("type", "hello")
                    .put("device", "${Build.MANUFACTURER} ${Build.MODEL}")
                    .put("version", 1)
                    .put("caps", org.json.JSONArray(CommandHandler.ACTIONS))
                    .toString())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val msg = try { JSONObject(text) } catch (_: Exception) { return }
                if (msg.optString("type") != "cmd") return
                val id = msg.optString("id")
                val action = msg.optString("action")
                val args = msg.optJSONObject("args") ?: JSONObject()
                worker.execute {
                    val r = try {
                        handler.run(action, args)
                    } catch (e: Exception) {
                        Result(false, "${e.javaClass.simpleName}: ${e.message}")
                    }
                    val out = JSONObject().put("type", "result").put("id", id)
                        .put("ok", r.ok).put("result", r.text)
                    if (r.data != null) out.put("data", r.data)
                    webSocket.send(out.toString())
                }
            }

            // Only the current socket may report a loss. connect() cancels the
            // previous socket, and its cancellation used to arrive here as a
            // failure, schedule another connect, cancel the good socket — a
            // reconnect every few seconds, forever.
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (webSocket === ws) lost(code)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (webSocket === ws) lost(response?.code ?: 0)
            }
        })
    }

    private fun lost(code: Int) {
        connected = false
        if (code == 4001 || code == 401) {
            // Unpaired on the PC (revoked): stop trying, ask for a new pairing.
            updateNotification("Desconectado — pareie de novo no app")
            Prefs.clear(this)
            stopSelf()
            return
        }
        updateNotification("PC fora de alcance — tentando de novo…")
        Tailscale.ensure(this)              // the usual cause away from home: VPN off (rate-limited)
        if (stopping) return
        // Out of reach for a while (away from home with Tailscale off, PC
        // switched off): try less and less often — up to every 5 minutes —
        // instead of every 30 s forever. A network change or opening the app
        // still reconnects at once.
        failures++
        scheduleReconnect(backoffMs)
        val cap = when {
            failures < 6 -> 30_000L
            failures < 12 -> 120_000L
            else -> 300_000L
        }
        backoffMs = (backoffMs * 2).coerceAtMost(cap)
    }

    private val reconnect = Runnable { connect() }

    /** At most one reconnect pending, whoever asks for it. */
    private fun scheduleReconnect(delayMs: Long) {
        main.removeCallbacks(reconnect)
        main.postDelayed(reconnect, delayMs)
    }

    /** Called by the notification listener for every new notification. */
    fun sendNotification(json: JSONObject) {
        if (connected) ws?.send(json.put("type", "notification").toString())
    }

    private fun createChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Conexão com o PC",
            NotificationManager.IMPORTANCE_LOW))
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL)
            .setContentTitle("MARK")
            .setContentText(text)
            .setSmallIcon(MaskIcon.small(connected))                 // outline = out of reach, solid = connected
            .setLargeIcon(MaskIcon.picture(this, connected))         // red/gold, or grey
            .setColor(MaskIcon.color(connected))
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)          // an update never rings, vibrates or pops up again
            .build()
    }

    private fun updateNotification(text: String) {
        // Re-posting the same text made HyperOS treat it as a new notification
        // every reconnect attempt — the "app opening by itself".
        if (text == lastText) return
        lastText = text
        getSystemService(NotificationManager::class.java).notify(1, buildNotification(text))
    }
}

data class Result(val ok: Boolean, val text: String, val data: JSONObject? = null)
