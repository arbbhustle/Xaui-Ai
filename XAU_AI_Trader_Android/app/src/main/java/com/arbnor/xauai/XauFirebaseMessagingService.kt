package com.arbnor.xauai

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

class XauFirebaseMessagingService : FirebaseMessagingService() {
    companion object { private const val CHANNEL_ID = "xau_trade_signals" }
    override fun onMessageReceived(message: RemoteMessage) {
        val raw = (message.data["direction"] ?: message.notification?.title.orEmpty()).uppercase()
        val direction = if ("SELL" in raw) "SELL" else if ("BUY" in raw) "BUY" else ""
        if (direction.isEmpty()) return
        val entry = message.data["entry"]?.toDoubleOrNull()
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "XAU BUY / SELL signals", NotificationManager.IMPORTANCE_HIGH)
        )
        val pending = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val body = message.data["body"] ?: if (entry != null)
            "XAU/USD " + direction + " · Entry " + String.format("%.2f", entry)
            else "XAU/USD " + direction + " setup"
        manager.notify(2001, NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("DardaniaXAUTRADE AI · " + direction)
            .setContentText(body).setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true).setContentIntent(pending).build())
    }
}
