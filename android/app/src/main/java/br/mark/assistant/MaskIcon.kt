package br.mark.assistant

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas

/**
 * The app's notification icon: a mask.
 *
 * The status bar only shows the icon's shape (Android paints every status-bar
 * icon in one colour), so the states differ by form there — outline when the
 * PC is out of reach, solid when connected. The colour goes where Android
 * allows it: the picture inside the notification, red and gold when connected,
 * grey when not.
 */
object MaskIcon {
    const val RED = 0xFFB3121F.toInt()
    const val GREY = 0xFF7A7F87.toInt()

    private val pictures = HashMap<Boolean, Bitmap>()

    fun small(connected: Boolean): Int =
        if (connected) R.drawable.ic_mask_on else R.drawable.ic_mask_off

    fun color(connected: Boolean): Int = if (connected) RED else GREY

    fun picture(c: Context, connected: Boolean): Bitmap = pictures.getOrPut(connected) {
        val d = c.getDrawable(if (connected) R.drawable.ic_mask_color else R.drawable.ic_mask_grey)!!
        val size = (64 * c.resources.displayMetrics.density).toInt()
        Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888).also { b ->
            d.setBounds(0, 0, size, size)
            d.draw(Canvas(b))
        }
    }
}
