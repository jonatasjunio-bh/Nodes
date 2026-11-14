package br.mark.assistant

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Reconnects to the PC after the phone restarts or the app is updated. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        MarkService.start(context)
    }
}
