package com.rulemind.core

import com.rulemind.core.models.Decision
import java.security.MessageDigest
import java.util.Collections
import java.util.LinkedHashMap
import java.util.concurrent.ConcurrentHashMap

class DecisionCache(
    private val maxEntries: Int,
    private val ttlMs: Long,
) {
    private data class CacheEntry(
        val key: String,
        val policyId: String,
        val decision: Decision,
        val expiresAt: Long,
    )

    private val entries = ConcurrentHashMap<String, CacheEntry>()
    private val accessOrder = Collections.synchronizedMap(
        object : LinkedHashMap<String, Long>(16, 0.75f, true) {
            override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, Long>?): Boolean {
                return false
            }
        },
    )

    @Synchronized
    fun get(key: String): Decision? {
        val entry = entries[key] ?: return null
        if (System.currentTimeMillis() > entry.expiresAt) {
            entries.remove(key)
            accessOrder.remove(key)
            return null
        }
        accessOrder[key] = System.currentTimeMillis()
        return entry.decision
    }

    @Synchronized
    fun put(policyId: String, key: String, decision: Decision) {
        entries[key] = CacheEntry(
            key = key,
            policyId = policyId,
            decision = decision,
            expiresAt = System.currentTimeMillis() + ttlMs,
        )
        accessOrder[key] = System.currentTimeMillis()
        evictIfNeeded()
    }

    @Synchronized
    fun clearAll() {
        entries.clear()
        accessOrder.clear()
    }

    @Synchronized
    fun clearForPolicy(policyId: String) {
        val toRemove = entries.values.filter { it.policyId == policyId }.map { it.key }
        toRemove.forEach {
            entries.remove(it)
            accessOrder.remove(it)
        }
    }

    fun buildKey(policyId: String, payloadJson: String, experimentVariant: String?): String {
        return sha256("$policyId|$payloadJson|${experimentVariant ?: ""}")
    }

    private fun evictIfNeeded() {
        while (entries.size > maxEntries) {
            val eldest = accessOrder.entries.firstOrNull()?.key ?: break
            accessOrder.remove(eldest)
            entries.remove(eldest)
        }
    }

    private fun sha256(value: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(value.toByteArray())
        return digest.joinToString("") { "%02x".format(it) }
    }
}
