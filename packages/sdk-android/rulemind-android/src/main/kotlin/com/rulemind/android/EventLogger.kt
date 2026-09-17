package com.rulemind.android

import android.content.Context
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import com.rulemind.core.models.RuleMindConfig
import com.rulemind.core.models.SdkEvent
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.util.concurrent.CopyOnWriteArrayList

class EventLogger(
    private val context: Context,
    private val config: RuleMindConfig,
    private val networkClient: NetworkClient,
) {
    // See SecurePrefs: security-crypto 1.1.0 deprecates EncryptedSharedPreferences and the
    // Jetpack Security crypto library has no successor, so this talks to the Keystore directly.
    private val prefs = SecurePrefs(context, STORE_NAME).also {
        LegacySecureStoreMigration.migrateIfNeeded(context, STORE_NAME, it)
    }
    private val pending = CopyOnWriteArrayList(loadPersisted())

    @Synchronized
    fun queue(type: String, data: Map<String, Any?> = emptyMap()) {
        pending += SdkEvent(type = type, timestamp = Instant.now().toString(), data = data)
        persist()
        if (pending.size >= config.eventFlushBatchSize) {
            flush()
        } else {
            scheduleFlush()
        }
    }

    @Synchronized
    fun flush() {
        if (pending.isEmpty()) {
            return
        }
        val events = pending.toList()
        if (runCatching { networkClient.uploadEvents(events) }.getOrDefault(false)) {
            pending.clear()
            persist()
        }
    }

    private fun scheduleFlush() {
        WorkManager.getInstance(context).enqueueUniqueWork(
            "rulemind-event-flush",
            ExistingWorkPolicy.REPLACE,
            OneTimeWorkRequestBuilder<SyncWorker>().build(),
        )
    }

    private fun persist() {
        val json = JSONArray()
        pending.forEach { event ->
            json.put(
                JSONObject()
                    .put("type", event.type)
                    .put("timestamp", event.timestamp)
                    .put("data", JSONObject(event.data)),
            )
        }
        prefs.putString(KEY_EVENTS, json.toString())
    }

    private fun loadPersisted(): List<SdkEvent> {
        val raw = prefs.getString(KEY_EVENTS, null) ?: return emptyList()
        return runCatching {
            val array = JSONArray(raw)
            buildList {
                for (index in 0 until array.length()) {
                    val item = array.optJSONObject(index) ?: continue
                    add(
                        SdkEvent(
                            type = item.optString("type"),
                            timestamp = item.optString("timestamp"),
                            data = BundleParser.run { item.optJSONObject("data")?.toMap() ?: emptyMap() },
                        ),
                    )
                }
            }
        }.getOrDefault(emptyList())
    }

    companion object {
        private const val STORE_NAME = "rulemind.events.store"
        private const val KEY_EVENTS = "events.pending"
    }
}
