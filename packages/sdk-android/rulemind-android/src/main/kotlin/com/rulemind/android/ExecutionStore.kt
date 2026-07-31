package com.rulemind.android

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.rulemind.core.models.Decision
import org.json.JSONArray
import org.json.JSONObject

internal class ExecutionStore(context: Context) {
    private val prefs = EncryptedSharedPreferences.create(
        context,
        "rulemind.executions.store",
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    fun save(decision: Decision) {
        val executionId = decision.executionId ?: return
        val ids = loadIds().toMutableSet()
        ids += executionId
        prefs.edit()
            .putString(keyFor(executionId), DecisionCodec.toJson(decision).toString())
            .putString(KEY_IDS, JSONArray(ids.toList()).toString())
            .apply()
    }

    /**
     * Persist only executions that are still resumable — paused for review, awaiting sync, or
     * carrying an undelivered pending operation. A terminal decision (completed with every
     * operation delivered) is pruned instead of stored, so this store can't grow without bound:
     * the durable decision outbox is the record of completed decisions. Called at every point
     * where an execution's lifecycle advances.
     */
    fun persist(decision: Decision) {
        val executionId = decision.executionId ?: return
        if (isResumable(decision)) save(decision) else delete(executionId)
    }

    private fun isResumable(decision: Decision): Boolean =
        decision.status != "completed" || decision.pendingOperations.any { it["status"] != "delivered" }

    fun get(executionId: String): Decision? {
        val raw = prefs.getString(keyFor(executionId), null) ?: return null
        return runCatching { DecisionCodec.fromJson(JSONObject(raw)) }.getOrNull()
    }

    fun list(): List<Decision> = loadIds().mapNotNull(::get)

    fun delete(executionId: String) {
        val ids = loadIds().toMutableSet()
        ids.remove(executionId)
        prefs.edit()
            .remove(keyFor(executionId))
            .putString(KEY_IDS, JSONArray(ids.toList()).toString())
            .apply()
    }

    private fun loadIds(): Set<String> {
        val raw = prefs.getString(KEY_IDS, null) ?: return emptySet()
        return runCatching {
            val array = JSONArray(raw)
            buildSet {
                for (index in 0 until array.length()) {
                    add(array.optString(index))
                }
            }
        }.getOrDefault(emptySet())
    }

    private fun keyFor(executionId: String) = "execution.$executionId"

    companion object {
        private const val KEY_IDS = "execution.ids"
    }
}
