package br.mark.assistant

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Bundle
import android.os.Process
import android.support.v4.media.MediaBrowserCompat.MediaItem
import android.support.v4.media.MediaDescriptionCompat
import android.support.v4.media.MediaMetadataCompat
import android.support.v4.media.session.MediaSessionCompat
import android.support.v4.media.session.PlaybackStateCompat
import androidx.core.app.NotificationCompat
import androidx.media.MediaBrowserServiceCompat

/**
 * MARK as a media app — which is how it appears in Android Auto, on the lock
 * screen and to the car's / headset's buttons:
 *
 *   ▶ (play)                start listening — talk to the assistant
 *   ⏸ / ⏹                   stop listening
 *   browse list             "Falar com o MARK" plus one-tap requests
 *                           (briefing, agenda, weather, news, messages)
 *   "Ok Google, tocar <x> no MARK"   <x> goes to the assistant as a request
 *
 * The screen shows what the assistant is doing and its last reply. The audio
 * itself is VoiceLink's. A foreground service (media playback + microphone),
 * so the voice keeps working with the screen off.
 */
class MarkMediaService : MediaBrowserServiceCompat() {

    companion object {
        private const val CHANNEL = "mark_voice"
        private const val NOTIF_ID = 2
        private const val ACTION_TOGGLE = "br.mark.assistant.TOGGLE_LISTEN"
        @Volatile var instance: MarkMediaService? = null

        fun start(c: Context) {
            if (!Prefs.paired(c) || !Prefs.running(c)) return
            try {
                c.startForegroundService(Intent(c, MarkMediaService::class.java))
            } catch (_: Exception) {
                // Not allowed from the background on some versions: Android Auto
                // binding to the service, or opening the app, starts it instead.
            }
        }

        // Who may browse / control MARK: the car, the system's media controls,
        // Google Assistant (voice "play … on MARK") and Bluetooth (AVRCP).
        private val TRUSTED = setOf(
            "com.google.android.projection.gearhead",
            "com.google.android.carassistant",
            "com.google.android.googlequicksearchbox",
            "com.android.systemui",
            "com.android.bluetooth",
            "com.google.android.bluetooth",
        )

        private val QUICK = listOf(
            Triple("briefing", "Resumo do dia", "Me dê o briefing do dia."),
            Triple("agenda", "Agenda de hoje", "Qual é a minha agenda de hoje?"),
            Triple("weather", "Previsão do tempo", "Como está o tempo hoje?"),
            Triple("news", "Notícias", "Quais são as principais notícias de hoje?"),
            Triple("messages", "Mensagens recentes", "Leia as minhas notificações recentes do celular."),
        )
    }

    private lateinit var session: MediaSessionCompat
    private val onVoice: () -> Unit = { update() }

    override fun onCreate() {
        super.onCreate()
        instance = this
        session = MediaSessionCompat(this, "MARK").apply {
            setCallback(callback)
            setSessionActivity(PendingIntent.getActivity(this@MarkMediaService, 0,
                Intent(this@MarkMediaService, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE))
            isActive = true
        }
        sessionToken = session.sessionToken
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, "Voz do assistente", NotificationManager.IMPORTANCE_LOW))
        goForeground()
        VoiceLink.start(this)
        VoiceLink.addListener(onVoice)
        update()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!Prefs.running(this)) { stopForeground(STOP_FOREGROUND_REMOVE); stopSelf(); return START_NOT_STICKY }
        if (intent?.action == ACTION_TOGGLE) VoiceLink.toggleListening()
        goForeground()
        return START_STICKY
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        MarkService.shutdown(this)
        super.onTaskRemoved(rootIntent)
    }

    override fun onUnbind(intent: Intent?): Boolean {
        if (VoiceLink.autoBound) { VoiceLink.autoBound = false; VoiceLink.refreshRoute() }
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        VoiceLink.removeListener(onVoice)
        VoiceLink.stop()
        session.release()
        instance = null
        super.onDestroy()
    }

    /**
     * Foreground with the microphone type when the app may use it (Android
     * only allows that when started while the app is on screen); otherwise as
     * plain media playback, and the mic type is added the next time the app
     * is opened (MainActivity calls start() again).
     */
    private var micForeground = false

    fun goForeground() {
        val mic = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        if (micForeground && mic) { notifyNow(); return }
        val n = buildNotification()
        if (Build.VERSION.SDK_INT >= 30) {
            val media = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
            val both = media or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            try {
                startForeground(NOTIF_ID, n, if (mic) both else media)
                micForeground = mic
            } catch (_: Exception) {
                try { startForeground(NOTIF_ID, n, media) } catch (_: Exception) {}
                micForeground = false
            }
        } else {
            try { startForeground(NOTIF_ID, n) } catch (_: Exception) {}
            micForeground = mic
        }
    }

    // ── browsing (the car's list) ────────────────────────────────────────────

    override fun onGetRoot(clientPackageName: String, clientUid: Int, rootHints: Bundle?): BrowserRoot? {
        val ok = clientPackageName == packageName || clientPackageName in TRUSTED ||
                clientUid == Process.SYSTEM_UID || clientUid == Process.myUid()
        if (!ok) return null
        if (clientPackageName == "com.google.android.projection.gearhead") {
            VoiceLink.autoBound = true
            VoiceLink.refreshRoute()
        }
        return BrowserRoot("root", null)
    }

    override fun onLoadChildren(parentId: String, result: Result<MutableList<MediaItem>>) {
        if (parentId != "root") { result.sendResult(mutableListOf()); return }
        val items = mutableListOf(item("talk", "🎙  Falar com o MARK", "Toque e fale"))
        QUICK.forEach { (id, title, _) -> items += item("cmd:$id", title, "Pedir ao MARK") }
        result.sendResult(items)
    }

    private fun item(id: String, title: String, subtitle: String) = MediaItem(
        MediaDescriptionCompat.Builder().setMediaId(id).setTitle(title).setSubtitle(subtitle).build(),
        MediaItem.FLAG_PLAYABLE)

    // ── controls ─────────────────────────────────────────────────────────────

    private val callback = object : MediaSessionCompat.Callback() {
        override fun onPlay() = VoiceLink.setListening(true)
        override fun onPause() = VoiceLink.setListening(false)
        override fun onStop() { VoiceLink.setListening(false); VoiceLink.flush() }

        override fun onPlayFromMediaId(mediaId: String?, extras: Bundle?) {
            if (mediaId == null || mediaId == "talk") { VoiceLink.setListening(true); return }
            QUICK.firstOrNull { "cmd:${it.first}" == mediaId }?.let { VoiceLink.say(it.third) }
        }

        override fun onPlayFromSearch(query: String?, extras: Bundle?) {
            val q = query?.trim().orEmpty()
            if (q.isEmpty()) VoiceLink.setListening(true) else VoiceLink.say(q)
        }
    }

    // ── what the car / lock screen shows ─────────────────────────────────────

    private fun statusLine(): String = when {
        !VoiceLink.connected -> "PC fora de alcance"
        VoiceLink.speaking -> "Falando…"
        VoiceLink.listening -> "Ouvindo — pode falar"
        else -> "Toque ▶ para falar"
    }

    private fun update() {
        val active = VoiceLink.listening || VoiceLink.speaking
        val reply = VoiceLink.lastReply.take(300)
        session.setPlaybackState(PlaybackStateCompat.Builder()
            .setState(if (active) PlaybackStateCompat.STATE_PLAYING else PlaybackStateCompat.STATE_PAUSED,
                PlaybackStateCompat.PLAYBACK_POSITION_UNKNOWN, 1f)
            .setActions(PlaybackStateCompat.ACTION_PLAY or PlaybackStateCompat.ACTION_PAUSE or
                    PlaybackStateCompat.ACTION_PLAY_PAUSE or PlaybackStateCompat.ACTION_STOP or
                    PlaybackStateCompat.ACTION_PLAY_FROM_MEDIA_ID or PlaybackStateCompat.ACTION_PLAY_FROM_SEARCH)
            .build())
        session.setMetadata(MediaMetadataCompat.Builder()
            .putString(MediaMetadataCompat.METADATA_KEY_TITLE, "MARK — ${statusLine()}")
            .putString(MediaMetadataCompat.METADATA_KEY_DISPLAY_TITLE, "MARK — ${statusLine()}")
            .putString(MediaMetadataCompat.METADATA_KEY_ARTIST, reply.ifEmpty { "Assistente pessoal" })
            .putString(MediaMetadataCompat.METADATA_KEY_DISPLAY_SUBTITLE, reply.ifEmpty { "Assistente pessoal" })
            .build())
        notifyNow()
    }

    private var lastShown = ""

    private fun notifyNow() {
        // only when what it shows changed: a reconnect loop must not re-post it
        val key = "${statusLine()}|${VoiceLink.lastReply.take(120)}|${VoiceLink.listening}|${VoiceLink.routeName}"
        if (key == lastShown) return
        lastShown = key
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, buildNotification())
    }

    private fun buildNotification(): Notification {
        val toggle = PendingIntent.getService(this, 1,
            Intent(this, MarkMediaService::class.java).setAction(ACTION_TOGGLE), PendingIntent.FLAG_IMMUTABLE)
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val listening = VoiceLink.listening
        val route = VoiceLink.routeName?.let { " · 🎧 $it" } ?: ""
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(MaskIcon.small(VoiceLink.connected))
            .setLargeIcon(MaskIcon.picture(this, VoiceLink.connected))
            .setColor(MaskIcon.color(VoiceLink.connected))
            .setContentTitle("MARK — ${statusLine()}")
            .setContentText(VoiceLink.lastReply.take(120).ifEmpty { "Assistente pessoal" } + route)
            .setContentIntent(open)
            .setOngoing(true)
            .setSilent(true)
            .setOnlyAlertOnce(true)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .addAction(if (listening) android.R.drawable.ic_media_pause else android.R.drawable.ic_btn_speak_now,
                if (listening) "Parar" else "Falar", toggle)
            .setStyle(androidx.media.app.NotificationCompat.MediaStyle()
                .setMediaSession(session.sessionToken)
                .setShowActionsInCompactView(0))
            .build()
    }
}
