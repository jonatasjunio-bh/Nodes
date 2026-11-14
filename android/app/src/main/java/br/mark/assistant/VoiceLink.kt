package br.mark.assistant

import android.app.UiModeManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.res.Configuration
import android.media.AudioAttributes
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * The assistant's voice on the phone, natively (wss://<pc>/ws/device-audio).
 *
 * The dashboard page used to do this with Web Audio, which only works while
 * the page is on screen and can't be reached from Android Auto. Here:
 *
 *  - replies play through an AudioTrack tagged USAGE_ASSISTANT, which Android
 *    routes like media: to the Bluetooth headset / car stereo when one is
 *    connected, to the car through Android Auto, else the phone's speaker.
 *    Music ducks while the assistant talks (transient audio focus).
 *  - the microphone streams 16 kHz PCM while "listening" is on — the phone's
 *    own mic, or the Bluetooth one (hands-free, call quality) if chosen.
 *  - when Bluetooth audio or car mode comes up the PC is told, and sends every
 *    reply here instead of to its own speakers.
 *
 * The mic is muted while a reply is playing (plus a short tail) so the car's
 * speakers can't feed the assistant its own voice.
 */
object VoiceLink {

    private const val OUT_RATE = 24000
    private const val IN_RATE = 16000
    private const val ECHO_TAIL_MS = 450L

    @Volatile var connected = false; private set
    @Volatile var listening = false; private set
    @Volatile var speaking = false; private set
    @Volatile var lastReply = ""; private set
    @Volatile var lastHeard = ""; private set
    @Volatile var routeName: String? = null; private set   // Bluetooth / car, or null
    @Volatile var autoBound = false                         // Android Auto is browsing us

    private lateinit var app: Context
    private lateinit var am: AudioManager
    private val main = Handler(Looper.getMainLooper())
    private val listeners = CopyOnWriteArraySet<() -> Unit>()
    private var started = false

    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()
    private var ws: WebSocket? = null
    private var backoffMs = 2000L

    // ── lifecycle ────────────────────────────────────────────────────────────

    fun start(c: Context) {
        if (started) return
        started = true
        app = c.applicationContext
        am = app.getSystemService(AudioManager::class.java)
        am.registerAudioDeviceCallback(deviceCallback, main)
        val f = IntentFilter().apply {
            addAction(UiModeManager.ACTION_ENTER_CAR_MODE)
            addAction(UiModeManager.ACTION_EXIT_CAR_MODE)
        }
        if (Build.VERSION.SDK_INT >= 33) app.registerReceiver(carReceiver, f, Context.RECEIVER_EXPORTED)
        else app.registerReceiver(carReceiver, f)
        playThread.start()
        refreshRoute()
        connect()
    }

    fun stop() {
        if (!started) return
        started = false
        setListening(false)
        try { am.unregisterAudioDeviceCallback(deviceCallback) } catch (_: Exception) {}
        try { app.unregisterReceiver(carReceiver) } catch (_: Exception) {}
        main.removeCallbacks(reconnect)
        ws?.close(1000, "bye"); ws = null
        connected = false
    }

    fun addListener(l: () -> Unit) { listeners.add(l) }
    fun removeListener(l: () -> Unit) { listeners.remove(l) }
    private fun changed() = main.post { listeners.forEach { try { it() } catch (_: Exception) {} } }

    // ── the link ─────────────────────────────────────────────────────────────

    private fun connect() {
        if (!started || !Prefs.paired(app)) return
        main.removeCallbacks(reconnect)
        val old = ws
        ws = null
        old?.cancel()
        val base = Prefs.baseUrl(app).replaceFirst("https://", "wss://")
        val req = Request.Builder().url("$base/ws/device-audio")
            .header("Authorization", "Bearer ${Prefs.token(app)}").build()
        ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (webSocket !== ws) return
                connected = true
                backoffMs = 2000L
                send(JSONObject().put("type", "speaker").put("on", Prefs.speakerOn(app)))
                sendRoute()
                if (listening) send(JSONObject().put("type", "listen").put("on", true))
                changed()
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                if (webSocket === ws) play(bytes.toByteArray())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                if (webSocket !== ws) return
                val m = try { JSONObject(text) } catch (_: Exception) { return }
                when (m.optString("type")) {
                    "flush" -> flush()
                    "log" -> {
                        val t = m.optString("text").trim()
                        if (t.isEmpty()) return
                        if (m.optString("speaker") == "user") lastHeard = t else lastReply = t
                        changed()
                    }
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (webSocket === ws) lost()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (webSocket === ws) lost()
            }
        })
    }

    private fun lost() {
        connected = false
        changed()
        if (!started) return
        main.removeCallbacks(reconnect)
        main.postDelayed(reconnect, backoffMs)
        backoffMs = (backoffMs * 2).coerceAtMost(30000L)
    }

    private val reconnect = Runnable { connect() }

    /** Reconnect now (network came back, or re-paired). */
    fun kick() = main.post { if (started && !connected) { backoffMs = 2000L; connect() } }

    private fun send(j: JSONObject) { ws?.send(j.toString()) }

    /** A command typed or picked on the car screen / notification. */
    fun say(text: String) {
        send(JSONObject().put("type", "say").put("text", text))
        lastHeard = text
        changed()
    }

    fun setSpeaker(on: Boolean) {
        Prefs.setSpeakerOn(app, on)
        send(JSONObject().put("type", "speaker").put("on", on))
        if (!on) flush()
    }

    // ── Bluetooth / car ──────────────────────────────────────────────────────

    private val BT_TYPES = buildSet {
        add(AudioDeviceInfo.TYPE_BLUETOOTH_A2DP)
        add(AudioDeviceInfo.TYPE_BLUETOOTH_SCO)
        if (Build.VERSION.SDK_INT >= 31) { add(AudioDeviceInfo.TYPE_BLE_HEADSET); add(AudioDeviceInfo.TYPE_BLE_SPEAKER) }
    }

    private fun btOutput(): AudioDeviceInfo? =
        am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).firstOrNull { it.type in BT_TYPES }

    private fun carMode(): Boolean =
        (app.resources.configuration.uiMode and Configuration.UI_MODE_TYPE_MASK) ==
                Configuration.UI_MODE_TYPE_CAR ||
                app.getSystemService(UiModeManager::class.java).currentModeType == Configuration.UI_MODE_TYPE_CAR

    fun refreshRoute() {
        if (!started) return
        val bt = btOutput()
        val name = when {
            autoBound || carMode() -> "Android Auto" + (bt?.productName?.let { " ($it)" } ?: "")
            bt != null -> bt.productName?.toString()?.ifBlank { null } ?: "Bluetooth"
            else -> null
        }
        if (name != routeName) {
            routeName = name
            sendRoute()
            changed()
        }
    }

    private fun sendRoute() =
        send(JSONObject().put("type", "route").put("bt", routeName != null).put("name", routeName ?: ""))

    private val deviceCallback = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(added: Array<out AudioDeviceInfo>) = refreshRoute()
        override fun onAudioDevicesRemoved(removed: Array<out AudioDeviceInfo>) = refreshRoute()
    }

    private val carReceiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) { main.postDelayed({ refreshRoute() }, 300) }
    }

    // ── playback ─────────────────────────────────────────────────────────────

    private val playQ = LinkedBlockingQueue<ByteArray>()
    @Volatile private var lastAudioAt = 0L
    @Volatile private var flushGen = 0
    private var focus: AudioFocusRequest? = null

    private fun play(pcm: ByteArray) {
        if (pcm.isNotEmpty()) playQ.offer(pcm)
    }

    fun flush() {
        flushGen++
        playQ.clear()
    }

    private fun attrs(): AudioAttributes {
        // Over a Bluetooth call link (hands-free mic) only voice-call audio is
        // carried. Otherwise MEDIA: the car / headset plays it on its stereo
        // media channel (A2DP, Android Auto's media stream) at full quality —
        // the assistant usage could land on the mono guidance channel, which
        // many cars play on the driver's side only.
        val usage = if (commDeviceOn) AudioAttributes.USAGE_VOICE_COMMUNICATION
                    else AudioAttributes.USAGE_MEDIA
        return AudioAttributes.Builder().setUsage(usage)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()
    }

    @Synchronized private fun takeFocus() {
        if (focus != null) return
        val r = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
            .setAudioAttributes(attrs())
            .setOnAudioFocusChangeListener { }
            .build()
        if (am.requestAudioFocus(r) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED) focus = r
    }

    @Synchronized private fun dropFocus() {
        focus?.let { am.abandonAudioFocusRequest(it) }
        focus = null
    }

    private val playThread = Thread({
        var track: AudioTrack? = null
        var gen = flushGen
        while (true) {
            val chunk = try { playQ.poll(150, TimeUnit.MILLISECONDS) } catch (_: InterruptedException) { null }
            if (gen != flushGen) {                       // interrupted: drop what is buffered
                gen = flushGen
                track?.let { try { it.pause(); it.flush(); it.play() } catch (_: Exception) {} }
            }
            if (chunk != null) {
                if (track == null) {
                    takeFocus()
                    track = newTrack()
                    if (!speaking) { speaking = true; changed() }
                }
                lastAudioAt = SystemClock.elapsedRealtime()
                val out = stereo(chunk)
                try { track?.write(out, 0, out.size) } catch (_: Exception) {}
            } else if (track != null && SystemClock.elapsedRealtime() - lastAudioAt > 500) {
                // Reply over (the last chunk has had time to drain): release.
                try { track.stop(); track.release() } catch (_: Exception) {}
                track = null
                speaking = false
                lastAudioAt = SystemClock.elapsedRealtime()
                if (!listening) { dropFocus(); releaseCommDevice() }
                changed()
            }
        }
    }, "mark-voice-out").apply { isDaemon = true }

    /** The PC sends mono; both channels carry it, so no car or headset
     *  can play it on one side only. */
    private fun stereo(mono: ByteArray): ByteArray {
        val out = ByteArray(mono.size * 2)
        var i = 0
        var o = 0
        while (i + 1 < mono.size) {
            out[o] = mono[i]; out[o + 1] = mono[i + 1]
            out[o + 2] = mono[i]; out[o + 3] = mono[i + 1]
            i += 2; o += 4
        }
        return out
    }

    private fun newTrack(): AudioTrack {
        val min = AudioTrack.getMinBufferSize(OUT_RATE, AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_16BIT)
        return AudioTrack.Builder()
            .setAudioAttributes(attrs())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(OUT_RATE)
                .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
            .setBufferSizeInBytes(maxOf(min, OUT_RATE / 5 * 4))      // ~200 ms
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build().apply { play() }
    }

    // ── microphone ───────────────────────────────────────────────────────────

    @Volatile private var micThread: Thread? = null
    @Volatile private var commDeviceOn = false

    fun toggleListening() = setListening(!listening)

    fun setListening(on: Boolean) {
        if (on == listening) return
        if (on && app.checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED) return
        listening = on
        send(JSONObject().put("type", "listen").put("on", on))
        if (on) {
            micThread = Thread({ micLoop() }, "mark-voice-in").apply { isDaemon = true; start() }
        } else {
            micThread = null
            if (!speaking) { dropFocus(); releaseCommDevice() }
        }
        changed()
    }

    private fun micLoop() {
        val me = Thread.currentThread()
        val useBt = Prefs.btMic(app) && btOutput() != null && useCommDevice()
        val source = if (useBt) MediaRecorder.AudioSource.VOICE_COMMUNICATION
                     else MediaRecorder.AudioSource.VOICE_RECOGNITION
        val min = AudioRecord.getMinBufferSize(IN_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = try {
            @Suppress("MissingPermission")
            AudioRecord(source, IN_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(min, 8192))
        } catch (e: Exception) { null }
        if (rec == null || rec.state != AudioRecord.STATE_INITIALIZED) {
            rec?.release()
            main.post { setListening(false) }
            return
        }
        takeFocus()                        // music pauses / ducks while you talk
        val buf = ByteArray(2048)          // 1024 samples = 64 ms, like the PC mic
        try {
            rec.startRecording()
            while (listening && micThread === me) {
                val n = rec.read(buf, 0, buf.size)
                if (n <= 0) continue
                val echo = speaking || SystemClock.elapsedRealtime() - lastAudioAt < ECHO_TAIL_MS
                if (!echo && connected) ws?.send(buf.toByteString(0, n))
            }
        } catch (_: Exception) {
        } finally {
            try { rec.stop() } catch (_: Exception) {}
            rec.release()
        }
    }

    /** Route the call-audio path to the Bluetooth headset / car (its mic). */
    private fun useCommDevice(): Boolean {
        if (commDeviceOn) return true
        return try {
            am.mode = AudioManager.MODE_IN_COMMUNICATION
            commDeviceOn = if (Build.VERSION.SDK_INT >= 31) {
                val dev = am.availableCommunicationDevices.firstOrNull {
                    it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || it.type == AudioDeviceInfo.TYPE_BLE_HEADSET
                }
                dev != null && am.setCommunicationDevice(dev)
            } else {
                @Suppress("DEPRECATION") am.startBluetoothSco()
                @Suppress("DEPRECATION") run { am.isBluetoothScoOn = true }
                true
            }
            if (!commDeviceOn) am.mode = AudioManager.MODE_NORMAL
            commDeviceOn
        } catch (_: Exception) {
            am.mode = AudioManager.MODE_NORMAL
            false
        }
    }

    private fun releaseCommDevice() {
        if (!commDeviceOn) return
        commDeviceOn = false
        try {
            if (Build.VERSION.SDK_INT >= 31) am.clearCommunicationDevice()
            else {
                @Suppress("DEPRECATION") run { am.isBluetoothScoOn = false }
                @Suppress("DEPRECATION") am.stopBluetoothSco()
            }
            am.mode = AudioManager.MODE_NORMAL
        } catch (_: Exception) {}
    }
}
