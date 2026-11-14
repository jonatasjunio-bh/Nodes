package br.mark.assistant

import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.SystemClock

/**
 * Brings Tailscale up in the background, so the PC is reachable away from home
 * without opening the Tailscale app first.
 *
 * Tailscale's Android app listens for com.tailscale.ipn.CONNECT_VPN on its
 * IPNReceiver (the hook it offers for Tasker-style automation): it connects
 * without showing any screen, as long as Android's VPN permission was granted
 * to it once before.
 */
object Tailscale {
    const val PKG = "com.tailscale.ipn"
    private const val ACTION_CONNECT = "com.tailscale.ipn.CONNECT_VPN"
    private const val RECEIVER = "com.tailscale.ipn.IPNReceiver"
    // A background nudge at most once a minute, and only while no VPN is up —
    // silent (no screen), so it can afford to be frequent: the VPN comes back
    // within a minute of dropping. A forced call (network change, opening the
    // app, the retry button) goes through at once.
    private const val MIN_GAP_MS = 60_000L

    @Volatile private var lastPoke = 0L

    fun installed(c: Context): Boolean = try {
        c.packageManager.getPackageInfo(PKG, 0); true
    } catch (_: Exception) { false }

    /** Any VPN up (Tailscale is the only one this phone is expected to run). */
    @Suppress("DEPRECATION")
    fun vpnUp(c: Context): Boolean {
        val cm = c.getSystemService(ConnectivityManager::class.java) ?: return false
        return cm.allNetworks.any {
            cm.getNetworkCapabilities(it)?.hasTransport(NetworkCapabilities.TRANSPORT_VPN) == true
        }
    }

    /** Ask Tailscale to connect, unless it already is or was just asked. */
    fun ensure(c: Context, force: Boolean = false) {
        if (!installed(c) || vpnUp(c)) return
        val now = SystemClock.elapsedRealtime()
        if (!force && now - lastPoke < MIN_GAP_MS) return
        lastPoke = now
        try {
            c.sendBroadcast(Intent(ACTION_CONNECT).setClassName(PKG, RECEIVER))
        } catch (_: Exception) {}
    }

    /** The Tailscale app itself — when the background request isn't enough
     *  (first use: Android still has to ask for the VPN permission there). */
    fun open(c: Context) {
        c.packageManager.getLaunchIntentForPackage(PKG)?.let {
            c.startActivity(it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        }
    }
}
