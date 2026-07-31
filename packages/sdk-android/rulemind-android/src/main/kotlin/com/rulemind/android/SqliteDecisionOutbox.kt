package com.rulemind.android

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import com.rulemind.core.DecisionOutbox
import com.rulemind.core.PendingDecision
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Durable, SQLite-backed [DecisionOutbox] using the Android framework SQLite directly (no Room
 * / KSP codegen). Optimised for a high-volume queue:
 *
 *  - **WAL** journal so the sync job reads while evaluate() writes, without lock contention;
 *  - an index on (next_attempt_at, seq) so eligibility + oldest-first draining and capacity
 *    trimming stay index-backed (no full scans) at 50k+ rows;
 *  - batch delete in one statement; PK-conflict-ignore = idempotent enqueue.
 *
 * All I/O runs on Dispatchers.IO so a decision write on evaluate() never touches the main
 * thread (no ANR) — that's why [DecisionOutbox] is a suspend interface.
 */
class SqliteDecisionOutbox(context: Context) : DecisionOutbox {
    private val helper = Helper(context.applicationContext)

    private class Helper(context: Context) :
        SQLiteOpenHelper(context, DB_NAME, null, DB_VERSION) {
        override fun onConfigure(db: SQLiteDatabase) {
            db.enableWriteAheadLogging()
        }

        override fun onCreate(db: SQLiteDatabase) {
            db.execSQL(
                """
                CREATE TABLE $TABLE (
                    id TEXT PRIMARY KEY,
                    policy_id TEXT,
                    outcome TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL DEFAULT 0,
                    seq INTEGER NOT NULL
                )
                """.trimIndent(),
            )
            db.execSQL("CREATE INDEX idx_${TABLE}_eligible ON $TABLE (next_attempt_at, seq)")
        }

        override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
            // v1 only for now.
        }
    }

    override suspend fun enqueue(decision: PendingDecision): Unit = withContext(Dispatchers.IO) {
        val values = ContentValues().apply {
            put("id", decision.id)
            put("policy_id", decision.policyId)
            put("outcome", decision.outcome)
            put("payload_json", decision.payloadJson)
            put("created_at", decision.createdAt)
            put("attempts", decision.attempts)
            put("next_attempt_at", decision.nextAttemptAtMs)
            put("seq", System.nanoTime()) // monotonic insertion order for oldest-first
        }
        // CONFLICT_IGNORE -> idempotent on id (PK): enqueuing the same decision twice is a no-op.
        helper.writableDatabase.insertWithOnConflict(TABLE, null, values, SQLiteDatabase.CONFLICT_IGNORE)
    }

    override suspend fun pending(limit: Int, nowMs: Long): List<PendingDecision> = withContext(Dispatchers.IO) {
        val out = ArrayList<PendingDecision>()
        helper.readableDatabase.query(
            TABLE, null, "next_attempt_at <= ?", arrayOf(nowMs.toString()), null, null, "seq ASC", limit.toString(),
        ).use { cursor ->
            val idIdx = cursor.getColumnIndexOrThrow("id")
            val policyIdx = cursor.getColumnIndexOrThrow("policy_id")
            val outcomeIdx = cursor.getColumnIndexOrThrow("outcome")
            val payloadIdx = cursor.getColumnIndexOrThrow("payload_json")
            val createdIdx = cursor.getColumnIndexOrThrow("created_at")
            val attemptsIdx = cursor.getColumnIndexOrThrow("attempts")
            val nextIdx = cursor.getColumnIndexOrThrow("next_attempt_at")
            while (cursor.moveToNext()) {
                out += PendingDecision(
                    id = cursor.getString(idIdx),
                    policyId = cursor.getString(policyIdx),
                    outcome = cursor.getString(outcomeIdx),
                    payloadJson = cursor.getString(payloadIdx),
                    createdAt = cursor.getString(createdIdx),
                    attempts = cursor.getInt(attemptsIdx),
                    nextAttemptAtMs = cursor.getLong(nextIdx),
                )
            }
        }
        out
    }

    override suspend fun markSynced(ids: List<String>): Unit = withContext(Dispatchers.IO) {
        if (ids.isEmpty()) return@withContext
        val placeholders = ids.joinToString(",") { "?" }
        helper.writableDatabase.delete(TABLE, "id IN ($placeholders)", ids.toTypedArray())
    }

    override suspend fun recordFailure(ids: List<String>, nextAttemptAtMs: Long): Unit = withContext(Dispatchers.IO) {
        if (ids.isEmpty()) return@withContext
        val placeholders = ids.joinToString(",") { "?" }
        helper.writableDatabase.execSQL(
            "UPDATE $TABLE SET attempts = attempts + 1, next_attempt_at = ? WHERE id IN ($placeholders)",
            arrayOf<Any>(nextAttemptAtMs, *ids.toTypedArray()),
        )
    }

    override suspend fun size(): Int = withContext(Dispatchers.IO) {
        helper.readableDatabase.rawQuery("SELECT COUNT(*) FROM $TABLE", null).use { cursor ->
            if (cursor.moveToFirst()) cursor.getInt(0) else 0
        }
    }

    override suspend fun trimToCapacity(maxRows: Int): Int = withContext(Dispatchers.IO) {
        val total = helper.readableDatabase.rawQuery("SELECT COUNT(*) FROM $TABLE", null).use {
            if (it.moveToFirst()) it.getInt(0) else 0
        }
        val overflow = total - maxRows
        if (overflow <= 0) return@withContext 0
        helper.writableDatabase.execSQL(
            "DELETE FROM $TABLE WHERE id IN (SELECT id FROM $TABLE ORDER BY seq ASC LIMIT ?)",
            arrayOf<Any>(overflow),
        )
        overflow
    }

    private companion object {
        const val DB_NAME = "rulemind_decisions.db"
        const val DB_VERSION = 1
        const val TABLE = "pending_decisions"
    }
}
