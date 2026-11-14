package br.mark.assistant

import android.Manifest
import android.app.NotificationManager
import android.app.SearchManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.location.Geocoder
import android.location.Location
import android.location.LocationManager
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.net.Uri
import android.os.BatteryManager
import android.os.Build
import android.os.CancellationSignal
import android.os.Handler
import android.os.Looper
import android.provider.AlarmClock
import android.provider.ContactsContract
import android.provider.MediaStore
import android.provider.Settings
import android.telephony.SmsManager
import android.view.KeyEvent
import org.json.JSONObject
import java.text.Normalizer
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Runs one assistant command with the phone's own APIs. Called on a worker thread. */
class CommandHandler(private val ctx: Context) {

    companion object {
        val ACTIONS = listOf(
            "flashlight", "volume", "ringer", "find_phone", "stop_ring", "alarm", "timer",
            "open_app", "open_url", "navigate", "battery", "location", "contacts", "call",
            "sms", "notifications", "reply", "media", "play_music", "wifi",
        )
        const val SPOTIFY = "com.spotify.music"
        const val WAZE = "com.waze"
        const val MAPS = "com.google.android.apps.maps"

        // Spoken names → the app they mean, when the label alone misleads
        // ("maps" also matches "Maps Go", "zap" matches nothing).
        private val APP_ALIASES = mapOf(
            "waze" to WAZE, "ways" to WAZE, "weiz" to WAZE, "weis" to WAZE, "wase" to WAZE, "uaze" to WAZE,
            "maps" to MAPS, "google maps" to MAPS, "mapa" to MAPS, "mapas" to MAPS,
            "whatsapp" to "com.whatsapp", "zap" to "com.whatsapp", "wpp" to "com.whatsapp",
            "spotify" to SPOTIFY, "youtube" to "com.google.android.youtube",
            "youtube music" to "com.google.android.apps.youtube.music",
            "instagram" to "com.instagram.android", "telegram" to "org.telegram.messenger",
            "uber" to "com.ubercab", "99" to "com.taxis99", "ifood" to "br.com.brainweb.ifood",
            "gmail" to "com.google.android.gm", "chrome" to "com.android.chrome",
            "netflix" to "com.netflix.mediaclient", "android auto" to "com.google.android.projection.gearhead",
        )
        private val NAV_APPS = setOf(
            WAZE, MAPS, "com.google.android.apps.mapslite", "com.sygic.aura", "com.here.app.maps",
            "com.huawei.maps.app", "net.osmand", "net.osmand.plus", "ru.yandex.yandexnavi",
            "ru.yandex.yandexmaps", "com.mapquest.android.ace", "cz.seznam.mapy", "com.tomtom.gplay.navapp",
            "com.tomtom.speedcams.android.map", "com.mapfactor.navigator", "menion.android.locus",
            "com.magicearth", "app.organicmaps",
        )
        private val HOME_WORDS = setOf("casa", "home", "minha casa", "para casa", "pra casa", "lar")
        private val WORK_WORDS = setOf("trabalho", "work", "servico", "meu trabalho", "escritorio")
    }

    private val main = Handler(Looper.getMainLooper())
    private val audio = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var ringer: MediaPlayer? = null
    private var savedAlarmVolume = -1

    fun release() = stopRing()

    fun run(action: String, a: JSONObject): Result = when (action) {
        "flashlight" -> flashlight(a.optBoolean("on", true))
        "volume" -> volume(a.optString("stream", "media"), a.opt("level"))
        "ringer" -> ringerMode(a.optString("mode", "normal"))
        "find_phone" -> findPhone(a.optInt("seconds", 30))
        "stop_ring" -> { stopRing(); Result(true, "Stopped ringing.") }
        "alarm" -> alarm(a.optInt("hour"), a.optInt("minute"), a.optString("label"))
        "timer" -> timer(a.optInt("seconds"), a.optString("label"))
        "open_app" -> openApp(a.optString("name"))
        "open_url" -> startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(a.optString("url"))), "Opened the link on the phone.")
        "navigate" -> navigate(a.optString("destination"), a.optString("app"))
        "battery" -> battery()
        "location" -> location()
        "contacts" -> contacts(a.optString("query"))
        "call" -> call(a.optString("to"))
        "sms" -> sms(a.optString("to"), a.optString("text"))
        "notifications" -> MarkNotificationListener.summary()
        "reply" -> MarkNotificationListener.reply(a.optString("key"), a.optString("text"))
        "media" -> mediaKey(a.optString("key", "play"))
        "wifi" -> wifi()
        "play_music" -> playMusic(a.optString("query"), a.optString("uri"))
        else -> Result(false, "unknown action '$action'")
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private fun granted(p: String) = ctx.checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    private fun need(p: String, what: String): Result? =
        if (granted(p)) null else Result(false, "the app has no permission for $what — open MARK on the phone → ⚙ Permissions")

    /**
     * Opening an activity from a background service is blocked on Android 10+
     * unless the app may draw over other apps — the setup screen asks for it.
     */
    private fun startActivity(i: Intent, ok: String): Result {
        if (!Settings.canDrawOverlays(ctx)) {
            return Result(false, "the phone blocks opening screens from the background — in MARK → ⚙ " +
                    "Permissions, allow 'Display over other apps' (on Xiaomi also 'Display pop-up windows " +
                    "while running in the background')")
        }
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            ctx.startActivity(i); Result(true, ok)
        } catch (e: Exception) {
            Result(false, "nothing on the phone can open that (${e.javaClass.simpleName})")
        }
    }

    private fun norm(s: String) = Normalizer.normalize(s.lowercase(Locale.ROOT), Normalizer.Form.NFD)
        .replace(Regex("\\p{M}+"), "").trim()

    // ── basic controls ───────────────────────────────────────────────────────

    private fun flashlight(on: Boolean): Result {
        val cm = ctx.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val id = cm.cameraIdList.firstOrNull {
            cm.getCameraCharacteristics(it).get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
        } ?: return Result(false, "this phone has no flashlight")
        cm.setTorchMode(id, on)
        return Result(true, if (on) "Flashlight on." else "Flashlight off.")
    }

    private fun volume(streamName: String, level: Any?): Result {
        val stream = when (streamName.lowercase()) {
            "ring", "ringtone", "toque" -> AudioManager.STREAM_RING
            "alarm", "alarme" -> AudioManager.STREAM_ALARM
            else -> AudioManager.STREAM_MUSIC
        }
        val max = audio.getStreamMaxVolume(stream)
        val cur = audio.getStreamVolume(stream)
        val target = when (val l = level?.toString()?.lowercase()) {
            "up" -> (cur + max / 6.0).toInt().coerceAtLeast(cur + 1)
            "down" -> (cur - max / 6.0).toInt().coerceAtMost(cur - 1)
            "max" -> max
            "mute" -> 0
            else -> ((l?.toIntOrNull() ?: return Result(false, "volume must be 0-100")) * max / 100.0).toInt()
        }.coerceIn(0, max)
        return try {
            audio.setStreamVolume(stream, target, AudioManager.FLAG_SHOW_UI)
            Result(true, "Phone ${streamName} volume ${target * 100 / max}%.")
        } catch (e: SecurityException) {
            Result(false, "the phone needs 'Do not disturb' access to change that — MARK → ⚙ Permissions")
        }
    }

    private fun ringerMode(mode: String): Result {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        if (mode != "normal" && !nm.isNotificationPolicyAccessGranted) {
            return Result(false, "silent mode needs 'Do not disturb' access — MARK → ⚙ Permissions")
        }
        audio.ringerMode = when (mode) {
            "silent" -> AudioManager.RINGER_MODE_SILENT
            "vibrate" -> AudioManager.RINGER_MODE_VIBRATE
            else -> AudioManager.RINGER_MODE_NORMAL
        }
        return Result(true, "Phone set to $mode.")
    }

    /** Rings at full alarm volume — works even in silent mode — to find the phone. */
    private fun findPhone(seconds: Int): Result {
        val latch = CountDownLatch(1)
        main.post {
            stopRing()
            savedAlarmVolume = audio.getStreamVolume(AudioManager.STREAM_ALARM)
            audio.setStreamVolume(AudioManager.STREAM_ALARM, audio.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0)
            val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
            ringer = MediaPlayer().apply {
                setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM).build())
                setDataSource(ctx, uri)
                isLooping = true
                prepare()
                start()
            }
            main.postDelayed({ stopRing() }, seconds.coerceIn(5, 120) * 1000L)
            latch.countDown()
        }
        latch.await(3, TimeUnit.SECONDS)
        return Result(true, "The phone is ringing for $seconds seconds.")
    }

    private fun stopRing() {
        main.post {
            ringer?.let { try { it.stop() } catch (_: Exception) {}; it.release() }
            ringer = null
            if (savedAlarmVolume >= 0) {
                audio.setStreamVolume(AudioManager.STREAM_ALARM, savedAlarmVolume, 0)
                savedAlarmVolume = -1
            }
        }
    }

    private fun alarm(hour: Int, minute: Int, label: String): Result =
        startActivity(Intent(AlarmClock.ACTION_SET_ALARM)
            .putExtra(AlarmClock.EXTRA_HOUR, hour)
            .putExtra(AlarmClock.EXTRA_MINUTES, minute)
            .putExtra(AlarmClock.EXTRA_MESSAGE, label.ifBlank { "MARK" })
            .putExtra(AlarmClock.EXTRA_SKIP_UI, true),
            "Alarm set for %02d:%02d on the phone.".format(hour, minute))

    private fun timer(seconds: Int, label: String): Result =
        startActivity(Intent(AlarmClock.ACTION_SET_TIMER)
            .putExtra(AlarmClock.EXTRA_LENGTH, seconds)
            .putExtra(AlarmClock.EXTRA_MESSAGE, label.ifBlank { "MARK" })
            .putExtra(AlarmClock.EXTRA_SKIP_UI, true),
            "Timer of ${seconds / 60} min ${seconds % 60} s started on the phone.")

    // ── finding apps by spoken name ─────────────────────────────────────────

    private data class App(val pkg: String, val label: String, val key: String)

    private fun launchable(): List<App> {
        val pm = ctx.packageManager
        return pm.queryIntentActivities(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER), 0)
            .map { it.activityInfo.packageName to it.loadLabel(pm).toString() }
            .distinctBy { it.first }
            .map { (pkg, label) -> App(pkg, label, norm(label)) }
    }

    /** Edit distance, for what speech recognition makes of app names ("ways" → Waze). */
    private fun dist(a: String, b: String): Int {
        val d = IntArray(b.length + 1) { it }
        for (i in 1..a.length) {
            var prev = d[0]; d[0] = i
            for (j in 1..b.length) {
                val cur = d[j]
                d[j] = minOf(d[j] + 1, d[j - 1] + 1, prev + if (a[i - 1] == b[j - 1]) 0 else 1)
                prev = cur
            }
        }
        return d[b.length]
    }

    /**
     * The apps a spoken name can mean, best tier only: an alias, the exact
     * label, a label word starting with it, the label containing it, every
     * word of it, then a near miss in spelling. More than one = ask.
     */
    private fun findApps(name: String): List<App> {
        val q = norm(name).removePrefix("o ").removePrefix("a ").removeSuffix(" app").trim()
        if (q.isEmpty()) return emptyList()
        val all = launchable()
        APP_ALIASES[q]?.let { pkg -> all.firstOrNull { it.pkg == pkg }?.let { return listOf(it) } }
        val words = { l: String -> l.split(' ', '-', '.', ':') }
        val tiers = listOf<(App) -> Boolean>(
            { it.key == q },
            { it.key.startsWith("$q ") || words(it.key).any { w -> w == q } },
            { words(it.key).any { w -> w.startsWith(q) } },
            { it.key.contains(q) },
            { a -> q.split(' ').filter { it.length > 2 }.let { w -> w.isNotEmpty() && w.all { a.key.contains(it) } } },
        )
        for (t in tiers) {
            val hit = all.filter(t)
            if (hit.isNotEmpty()) return hit.distinctBy { it.key }
        }
        // near misses: the whole label or one of its words, within ~1 edit per 4 letters
        val tol = maxOf(1, q.length / 4)
        val near = all.map { a -> a to (listOf(a.key) + words(a.key)).minOf { dist(q, it) } }
            .filter { it.second <= tol }
        val best = near.minOfOrNull { it.second } ?: return emptyList()
        return near.filter { it.second == best }.map { it.first }.distinctBy { it.key }
    }

    private fun askWhich(what: String, apps: List<App>, retry: String): Result {
        val names = apps.map { it.label }.distinct()
        return Result(true, "[ASK THE USER] $what: ${names.joinToString(", ")}. Ask which one " +
                "(one short question, listing them), then call again with $retry.",
            JSONObject().put("choices", org.json.JSONArray(names)))
    }

    private fun openApp(name: String): Result {
        val pm = ctx.packageManager
        val hits = findApps(name)
        if (hits.isEmpty()) return Result(false, "no app called '$name' on the phone")
        if (hits.size > 1) return askWhich("Several apps on the phone match '$name'", hits,
            "open_app value=<the exact name chosen>")
        val app = hits[0]
        val launch = pm.getLaunchIntentForPackage(app.pkg)
            ?: return Result(false, "'${app.label}' cannot be opened")
        return startActivity(launch, "Opened ${app.label} on the phone.")
    }

    /** A media button, as a headset would press it: goes to whichever app
     *  played last (Spotify, YouTube Music…). */
    private fun mediaKey(which: String): Result {
        val code = when (which.lowercase()) {
            "pause" -> KeyEvent.KEYCODE_MEDIA_PAUSE
            "toggle", "play_pause" -> KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE
            "next" -> KeyEvent.KEYCODE_MEDIA_NEXT
            "previous", "prev" -> KeyEvent.KEYCODE_MEDIA_PREVIOUS
            "stop" -> KeyEvent.KEYCODE_MEDIA_STOP
            else -> KeyEvent.KEYCODE_MEDIA_PLAY
        }
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, code))
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_UP, code))
        return Result(true, "Sent '$which' to the phone's music player.")
    }

    /**
     * Spotify on the phone, playing — used when the PC can't start it through
     * Spotify Connect (the phone's Spotify not registered yet, or no Premium).
     *   uri    spotify:track:… / album / playlist / artist — opened, then ▶
     *   query  "Legião Urbana" — Spotify's own play-from-search (starts by itself)
     *   none   open Spotify and ▶ (resume what it had)
     */
    private fun playMusic(query: String, uri: String): Result {
        if (ctx.packageManager.getLaunchIntentForPackage(SPOTIFY) == null)
            return Result(false, "Spotify is not installed on the phone")
        val opened = when {
            uri.isNotEmpty() -> startActivity(
                Intent(Intent.ACTION_VIEW, Uri.parse(uri)).setPackage(SPOTIFY), "")
            query.isNotEmpty() -> startActivity(
                Intent(MediaStore.INTENT_ACTION_MEDIA_PLAY_FROM_SEARCH).setPackage(SPOTIFY)
                    .putExtra(SearchManager.QUERY, query)
                    .putExtra(MediaStore.EXTRA_MEDIA_FOCUS, "vnd.android.cursor.item/*"), "")
            else -> startActivity(ctx.packageManager.getLaunchIntentForPackage(SPOTIFY)!!, "")
        }
        if (!opened.ok) return opened
        if (query.isEmpty()) {
            // Opening a page doesn't start it: press play once Spotify is up.
            Thread.sleep(2500)
            mediaKey("play")
        }
        return Result(true, "Spotify is playing on the phone" +
                (if (query.isNotEmpty()) " ('$query')." else "."))
    }

    /** The navigation apps on the phone (driving): the known navigators
     *  that are installed. */
    private fun navApps(): List<App> {
        val pm = ctx.packageManager
        val all = launchable().associateBy { it.pkg }
        val pkgs = LinkedHashSet<String>()
        listOf(WAZE, MAPS).forEach { if (it in all) pkgs += it }
        // only real navigators: ride apps (Uber, 99) take these links too
        pm.queryIntentActivities(Intent(Intent.ACTION_VIEW, Uri.parse("google.navigation:q=x")), 0)
            .map { it.activityInfo.packageName }.filter { it in NAV_APPS }.forEach { pkgs += it }
        pm.queryIntentActivities(Intent(Intent.ACTION_VIEW, Uri.parse("geo:0,0?q=x")), 0)
            .map { it.activityInfo.packageName }.filter { it in NAV_APPS }.forEach { pkgs += it }
        return pkgs.mapNotNull { all[it] }.distinctBy { it.key }
    }

    /**
     * Turn-by-turn in the app asked for. Without one: the only navigation app
     * there is, or — with several — the user is asked which. "casa" /
     * "trabalho" go to the app's saved Home / Work (Waze, Google Maps).
     */
    private fun navigate(dest: String, appReq: String): Result {
        val navs = navApps()
        if (navs.isEmpty()) return startActivity(
            Intent(Intent.ACTION_VIEW, Uri.parse("geo:0,0?q=" + Uri.encode(dest))), "Opened $dest on the phone's map.")
        val asked = norm(appReq)
        val app: App = when {
            asked.isEmpty() -> if (navs.size == 1) navs[0] else return askWhich(
                "Navigation apps on the phone", navs, "navigate value=<destination> app=<the app chosen>")
            asked.contains("waze") -> navs.firstOrNull { it.pkg == WAZE }
            asked == "maps" || asked.contains("google") -> navs.firstOrNull { it.pkg == MAPS }
            else -> null
        } ?: run {
            // any other app by name — a navigation one first, else whatever matches
            val hits = findApps(appReq)
            val navHits = hits.filter { h -> navs.any { it.pkg == h.pkg } }
            when {
                navHits.size == 1 -> navHits[0]
                navHits.size > 1 -> return askWhich("Several navigation apps match '$appReq'", navHits,
                    "navigate value=<destination> app=<the app chosen>")
                hits.size == 1 -> hits[0]
                hits.size > 1 -> return askWhich("Several apps match '$appReq'", hits,
                    "navigate value=<destination> app=<the app chosen>")
                else -> return Result(false, "no app called '$appReq' on the phone — navigation apps " +
                        "there: ${navs.joinToString(", ") { it.label }}")
            }
        }
        val d = norm(dest).removePrefix("para ").removePrefix("pra ").removePrefix("a ").trim()
        val fav = when (d) { in HOME_WORDS -> "home"; in WORK_WORDS -> "work"; else -> "" }
        val shown = when (fav) { "home" -> "casa"; "work" -> "o trabalho"; else -> dest }
        val ok = "Navigation to $shown started in ${app.label}."
        return when (app.pkg) {
            WAZE -> startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(
                if (fav.isNotEmpty()) "waze://?favorite=$fav&navigate=yes"
                else "https://waze.com/ul?q=${Uri.encode(dest)}&navigate=yes")).setPackage(WAZE), ok)
            MAPS -> startActivity(Intent(Intent.ACTION_VIEW,
                Uri.parse("google.navigation:q=" + Uri.encode(fav.ifEmpty { dest })))
                .setPackage(MAPS), ok)
            else -> {
                val pm = ctx.packageManager
                val navI = Intent(Intent.ACTION_VIEW, Uri.parse("google.navigation:q=" + Uri.encode(dest))).setPackage(app.pkg)
                val geoI = Intent(Intent.ACTION_VIEW, Uri.parse("geo:0,0?q=" + Uri.encode(dest))).setPackage(app.pkg)
                when {
                    navI.resolveActivity(pm) != null -> startActivity(navI, ok)
                    geoI.resolveActivity(pm) != null -> startActivity(geoI, "Opened $shown in ${app.label}.")
                    else -> pm.getLaunchIntentForPackage(app.pkg)?.let {
                        startActivity(it, "${app.label} opened — it takes no destination from other apps, " +
                                "so the user types '$dest' there.")
                    } ?: Result(false, "'${app.label}' cannot be opened")
                }
            }
        }
    }

    private fun battery(): Result {
        val bm = ctx.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val pct = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        val st = ctx.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val status = st?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
        val charging = status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL
        return Result(true, "Phone battery $pct%" + if (charging) ", charging." else ".",
            JSONObject().put("percent", pct).put("charging", charging))
    }

    /** Wi-Fi signal strength, for the HUD's antenna hologram. */
    @Suppress("DEPRECATION")
    private fun wifi(): Result {
        val wm = ctx.applicationContext.getSystemService(Context.WIFI_SERVICE) as android.net.wifi.WifiManager
        val info = wm.connectionInfo
        if (info == null || info.networkId == -1) return Result(false, "the phone is not on Wi-Fi")
        val rssi = info.rssi
        val pct = ((rssi + 100) * 2).coerceIn(0, 100)          // -100 dBm → 0 %, -50 dBm → 100 %
        val ssid = info.ssid?.trim('"')?.takeIf { it.isNotEmpty() && it != "<unknown ssid>" } ?: ""
        return Result(true, "Phone Wi-Fi signal $pct% ($rssi dBm)" + (if (ssid.isNotEmpty()) " on $ssid." else "."),
            JSONObject().put("percent", pct).put("rssi", rssi).put("ssid", ssid))
    }

    // ── location ─────────────────────────────────────────────────────────────

    @Suppress("DEPRECATION")
    private fun location(): Result {
        need(Manifest.permission.ACCESS_FINE_LOCATION, "location")?.let { return it }
        val lm = ctx.getSystemService(Context.LOCATION_SERVICE) as LocationManager
        var loc: Location? = null
        if (Build.VERSION.SDK_INT >= 30) {
            val provider = when {
                Build.VERSION.SDK_INT >= 31 && lm.isProviderEnabled(LocationManager.FUSED_PROVIDER) -> LocationManager.FUSED_PROVIDER
                lm.isProviderEnabled(LocationManager.GPS_PROVIDER) -> LocationManager.GPS_PROVIDER
                else -> LocationManager.NETWORK_PROVIDER
            }
            val latch = CountDownLatch(1)
            val cancel = CancellationSignal()
            try {
                lm.getCurrentLocation(provider, cancel, ctx.mainExecutor) { l -> loc = l; latch.countDown() }
                if (!latch.await(20, TimeUnit.SECONDS)) cancel.cancel()
            } catch (_: Exception) {}
        }
        if (loc == null) {
            loc = listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER, "passive")
                .mapNotNull { try { lm.getLastKnownLocation(it) } catch (_: Exception) { null } }
                .maxByOrNull { it.time }
        }
        val l = loc ?: return Result(false, "the phone could not get a location (is location turned on?)")
        var address = ""
        try {
            Geocoder(ctx, Locale.getDefault()).getFromLocation(l.latitude, l.longitude, 1)
                ?.firstOrNull()?.let { a -> address = a.getAddressLine(0) ?: "" }
        } catch (_: Exception) {}
        val ageMin = (System.currentTimeMillis() - l.time) / 60000
        val text = (if (address.isNotEmpty()) "The phone is at $address" else
            "The phone is at %.5f, %.5f".format(l.latitude, l.longitude)) +
                " (±${l.accuracy.toInt()} m" + (if (ageMin > 2) ", $ageMin min ago" else "") + ")."
        return Result(true, text, JSONObject().put("lat", l.latitude).put("lon", l.longitude)
            .put("accuracy", l.accuracy).put("address", address))
    }

    // ── contacts, calls, SMS ─────────────────────────────────────────────────

    private fun findContacts(query: String, limit: Int = 5): List<Pair<String, String>> {
        if (!granted(Manifest.permission.READ_CONTACTS)) return emptyList()
        val out = mutableListOf<Pair<String, String>>()
        val q = norm(query)
        ctx.contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            arrayOf(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
                ContactsContract.CommonDataKinds.Phone.NUMBER),
            null, null, ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " ASC"
        )?.use { c ->
            while (c.moveToNext() && out.size < 200) {
                val name = c.getString(0) ?: continue
                val num = c.getString(1) ?: continue
                if (norm(name).contains(q)) out.add(name to num)
            }
        }
        // Exact name first, then names starting with the query.
        return out.distinctBy { it.first }
            .sortedBy { val n = norm(it.first); if (n == q) 0 else if (n.startsWith(q)) 1 else 2 }
            .take(limit)
    }

    private fun contacts(query: String): Result {
        need(Manifest.permission.READ_CONTACTS, "contacts")?.let { return it }
        val hits = findContacts(query)
        return if (hits.isEmpty()) Result(false, "no contact matching '$query'")
        else Result(true, "Contacts: " + hits.joinToString("; ") { "${it.first} ${it.second}" })
    }

    /** A number as given, or the best contact match. Ambiguous names are refused. */
    private fun resolveNumber(to: String): Pair<String?, String> {
        val digits = to.filter { it.isDigit() || it == '+' }
        if (digits.count { it.isDigit() } >= 8) return digits to to
        val hits = findContacts(to)
        if (hits.isEmpty()) return null to "no contact called '$to'"
        val exact = hits.filter { norm(it.first) == norm(to) }
        val pick = exact.singleOrNull() ?: hits.singleOrNull()
            ?: return null to ("'$to' matches several contacts: " + hits.joinToString(", ") { it.first } +
                    " — say the full name")
        return pick.second to pick.first
    }

    private fun call(to: String): Result {
        need(Manifest.permission.CALL_PHONE, "phone calls")?.let { return it }
        val (num, who) = resolveNumber(to)
        num ?: return Result(false, who)
        return startActivity(Intent(Intent.ACTION_CALL, Uri.parse("tel:" + Uri.encode(num))),
            "Calling $who from the phone.")
    }

    private fun sms(to: String, text: String): Result {
        need(Manifest.permission.SEND_SMS, "SMS")?.let { return it }
        if (text.isBlank()) return Result(false, "the message is empty")
        val (num, who) = resolveNumber(to)
        num ?: return Result(false, who)
        val sm = if (Build.VERSION.SDK_INT >= 31) ctx.getSystemService(SmsManager::class.java)
                 else @Suppress("DEPRECATION") SmsManager.getDefault()
        sm.sendMultipartTextMessage(num, null, sm.divideMessage(text), null, null)
        return Result(true, "SMS sent to $who.")
    }
}
