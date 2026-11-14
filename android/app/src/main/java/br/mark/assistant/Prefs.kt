package br.mark.assistant

import android.content.Context

/** Where the PC is and the permanent device token from pairing. Private to the app. */
object Prefs {
    private const val FILE = "mark"

    private fun sp(c: Context) = c.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun baseUrl(c: Context): String = sp(c).getString("base", "") ?: ""
    fun token(c: Context): String = sp(c).getString("token", "") ?: ""
    fun paired(c: Context): Boolean = baseUrl(c).isNotEmpty() && token(c).isNotEmpty()

    fun save(c: Context, base: String, token: String) {
        sp(c).edit().putString("base", base.trimEnd('/')).putString("token", token).apply()
    }

    // Voice: replies play on the phone (vs. the PC), and with a Bluetooth
    // headset / car connected, whether to also use its microphone (call-quality
    // audio, but hands-free) instead of the phone's own.
    fun speakerOn(c: Context): Boolean = sp(c).getBoolean("speaker", true)
    fun setSpeakerOn(c: Context, on: Boolean) = sp(c).edit().putBoolean("speaker", on).apply()
    // "bt_mic2": the old switch is not carried over — it put every reply on
    // the hands-free (call) channel: mono, low quality, often one speaker.
    fun btMic(c: Context): Boolean = sp(c).getBoolean("bt_mic2", false)
    fun setBtMic(c: Context, on: Boolean) = sp(c).edit().putBoolean("bt_mic2", on).apply()


    // Whether MARK should be running in the background. False once the user
    // closes it (swipes it out of Recents): nothing restarts it — not Android's
    // sticky-service restart, not boot, not an update — until it is opened.
    fun running(c: Context): Boolean = sp(c).getBoolean("running", true)
    fun setRunning(c: Context, on: Boolean) = sp(c).edit().putBoolean("running", on).commit()

    fun clear(c: Context) = sp(c).edit().clear().apply()
}
