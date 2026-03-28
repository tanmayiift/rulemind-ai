package com.rulemind.sample

import android.os.Bundle
import android.view.Gravity
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.rulemind.android.RuleMind
import com.rulemind.core.models.RuleMindConfig

class MainActivity : AppCompatActivity() {
    private lateinit var logView: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 48, 32, 48)
        }

        val title = TextView(this).apply {
            text = "RuleMind SDK Sample"
            textSize = 22f
            setTypeface(typeface, android.graphics.Typeface.BOLD)
            gravity = Gravity.CENTER
        }
        root.addView(title)

        val subtitle = TextView(this).apply {
            text = "Enterprise decisioning SDK demo"
            textSize = 13f
            gravity = Gravity.CENTER
            setPadding(0, 8, 0, 32)
        }
        root.addView(subtitle)

        val buttonRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
        }
        val syncButton = Button(this).apply { text = "Sync Bundle" }
        val evaluateButton = Button(this).apply { text = "Evaluate" }
        val eventButton = Button(this).apply { text = "Log Event" }
        val clearButton = Button(this).apply { text = "Clear Cache" }
        buttonRow.addView(syncButton)
        buttonRow.addView(evaluateButton)
        buttonRow.addView(eventButton)
        buttonRow.addView(clearButton)
        root.addView(buttonRow)

        logView = TextView(this).apply {
            textSize = 12f
            setPadding(0, 24, 0, 0)
            setTextIsSelectable(true)
        }
        val scrollView = ScrollView(this).apply {
            addView(logView)
        }
        root.addView(scrollView, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.MATCH_PARENT,
            1f,
        ))

        setContentView(root)

        // --- SDK initialization ---
        val baseUrl = intent.getStringExtra("RULEMIND_BASE_URL") ?: "https://api.rulemind.local"
        val apiKey = intent.getStringExtra("RULEMIND_API_KEY") ?: "rm_live_your_key_here"

        try {
            RuleMind.initialize(
                context = applicationContext,
                config = RuleMindConfig(
                    baseUrl = baseUrl,
                    apiKey = apiKey,
                ),
            )
            appendLog("SDK initialized (baseUrl=$baseUrl)")
        } catch (e: Exception) {
            appendLog("ERROR initializing SDK: ${e.message}")
        }

        // --- Sync bundle from server ---
        syncButton.setOnClickListener {
            Thread {
                try {
                    appendLog("Syncing bundle...")
                    val bundle = RuleMind.syncNow()
                    if (bundle != null) {
                        appendLog("Bundle synced: v${bundle.bundleVersion}, ${bundle.policies.size} policies, ${bundle.variables.size} variables")
                    } else {
                        appendLog("No new bundle (already up to date or server unreachable)")
                    }
                } catch (e: Exception) {
                    appendLog("ERROR syncing: ${e.message}")
                }
            }.start()
        }

        // --- Evaluate a policy ---
        evaluateButton.setOnClickListener {
            Thread {
                try {
                    val payload = mapOf(
                        "bureau" to mapOf("score" to 720),
                        "bank" to mapOf("summary" to mapOf("avgBalance" to 45200)),
                        "device" to mapOf("risk_score" to 0),
                    )
                    appendLog("Evaluating policy 'policy_pl_underwriting'...")
                    val decision = RuleMind.evaluate(
                        policyId = "policy_pl_underwriting",
                        payload = payload,
                        userId = "user_demo_123",
                    )
                    appendLog("Decision: outcome=${decision.outcome}, score=${decision.score}, latency=${decision.latencyMs}ms")
                    appendLog("  Variables: ${decision.variables}")
                    appendLog("  Rules: ${decision.ruleResults.size} evaluated")
                    if (decision.serverOnlyStepsSkipped.isNotEmpty()) {
                        appendLog("  Server-only steps skipped: ${decision.serverOnlyStepsSkipped}")
                    }
                } catch (e: Exception) {
                    appendLog("ERROR evaluating: ${e.message}")
                }
            }.start()
        }

        // --- Log an event ---
        eventButton.setOnClickListener {
            try {
                RuleMind.logEvent("sample_app.button_pressed", mapOf(
                    "screen" to "main",
                    "action" to "evaluate_demo",
                ))
                appendLog("Event queued: sample_app.button_pressed")
                RuleMind.flushEvents()
                appendLog("Events flushed")
            } catch (e: Exception) {
                appendLog("ERROR logging event: ${e.message}")
            }
        }

        // --- Clear cache ---
        clearButton.setOnClickListener {
            try {
                RuleMind.clearCache()
                appendLog("Decision cache cleared")
            } catch (e: Exception) {
                appendLog("ERROR clearing cache: ${e.message}")
            }
        }
    }

    private fun appendLog(message: String) {
        runOnUiThread {
            logView.append("${message}\n\n")
        }
    }
}
