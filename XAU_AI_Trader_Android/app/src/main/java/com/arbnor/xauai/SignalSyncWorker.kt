package com.arbnor.xauai

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class SignalSyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    companion object {
        const val UNIQUE_WORK = "xau-signal-background-sync"
        private const val CHANNEL_ID = "xau_trade_signals"
        private const val PREFS = "xauai"
        private const val LAST_NOTIFIED = "last_notified_signal"
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        try {
            val prefs = applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val endpoint = prefs.getString("endpoint", ApiClient.DEFAULT_ENDPOINT) ?: ApiClient.DEFAULT_ENDPOINT
            val dashboard = ApiClient().fetch(endpoint)
            val signal = dashboard.signal
            val direction = Presentation.direction(signal)
            if (direction != "BUY" && direction != "SELL") return@withContext Result.success()

            val fields = Fields(signal)
            // research-signal.timestamp_utc/served_at changes on every API read, so it
            // cannot identify a signal. Deduplicate on the originating XAU 5m setup.
            val signalClose = fields.text("xau.signal_candle_close", "signal_candle_close")
                ?: return@withContext Result.success()
            val expiresAt = fields.text("xau.expires_at", "expires_at")
                ?: return@withContext Result.success()
            val entry = fields.number("entry", "xau.entry")
            val key = direction + "|" + signalClose + "|" + expiresAt
            if (prefs.getString(LAST_NOTIFIED, null) == key) return@withContext Result.success()

            notifySignal(direction, entry)
            prefs.edit().putString(LAST_NOTIFIED, key).apply()
            Result.success()
        } catch (_: ApiFailure) {
            Result.retry()
        } catch (_: Exception) {
            Result.retry()
        }
    }

    private fun notifySignal(direction: String, entry: Double?) {
        val manager = applicationContext.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "XAU BUY / SELL signals", NotificationManager.IMPORTANCE_HIGH)
            )
        }
        val intent = Intent(applicationContext, MainActivity::class.java)
        val pending = PendingIntent.getActivity(
            applicationContext, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val body = if (entry != null) "XAU/USD " + direction + " · Entry " + String.format("%.2f", entry)
                   else "XAU/USD " + direction + " setup"
        val notification = NotificationCompat.Builder(applicationContext, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("DardaniaXAUTRADE AI · " + direction)
            .setContentText(body)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(pending)
            .build()
        manager.notify(1001, notification)
    }
}
