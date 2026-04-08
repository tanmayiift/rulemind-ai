package com.rulemind.sample

import android.content.Context
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextClearance
import androidx.compose.ui.test.performTextInput
import androidx.lifecycle.ViewModelProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.rulemind.sample.viewmodel.AppState
import com.rulemind.sample.viewmodel.RuleMindViewModel
import org.junit.Assume.assumeTrue
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import androidx.compose.ui.test.ComposeTimeoutException
import java.net.URL

@RunWith(AndroidJUnit4::class)
class StudioAppFlowTest {
    @get:Rule
    val composeRule = createAndroidComposeRule<MainActivity>()

    @Test
    fun demoScenarioSupportsReviewResumeAndAudit() {
        resetAppState()

        composeRule.onNodeWithTag("action_enter_demo", useUnmergedTree = true).performClick()
        waitForTag("route_experience_selection")
        composeRule.onNodeWithTag("action_open_home_dashboard", useUnmergedTree = true).performClick()
        waitForTag("route_home_dashboard")
        composeRule.onNodeWithTag("action_open_scenarios", useUnmergedTree = true).performClick()
        waitForTag("route_scenario_hub")
        composeRule.onNodeWithTag("scenario_card_travel_senior_review", useUnmergedTree = true).performScrollTo()
        composeRule.onNodeWithTag("action_run_scenario_travel_senior_review", useUnmergedTree = true).performClick()
        waitForDecision("travel_guard")
        waitForRoute("travel_decision")
        waitForTag("decision_summary")
        composeRule.onNodeWithTag("action_review_approve", useUnmergedTree = true).performScrollTo()
        composeRule.onNodeWithTag("action_review_approve", useUnmergedTree = true).assertIsDisplayed().performClick()
        composeRule.waitUntil(15_000) {
            composeRule.onAllNodesWithText("Status: completed", substring = true).fetchSemanticsNodes().isNotEmpty()
        }
        waitForRoute("travel_audit")
        waitForTag("trace_item_0")
        composeRule.onNodeWithTag("trace_item_0", useUnmergedTree = true).assertIsDisplayed()
    }

    @Test
    fun demoScenarioShowsAndFlushesPendingCallbacks() {
        resetAppState()

        composeRule.onNodeWithTag("action_enter_demo", useUnmergedTree = true).performClick()
        waitForTag("route_experience_selection")
        composeRule.onNodeWithTag("action_open_home_dashboard", useUnmergedTree = true).performClick()
        waitForTag("route_home_dashboard")
        composeRule.onNodeWithTag("action_open_scenarios", useUnmergedTree = true).performClick()
        waitForTag("route_scenario_hub")
        composeRule.onNodeWithTag("scenario_card_loan_prime_approved", useUnmergedTree = true).performScrollTo()
        composeRule.onNodeWithTag("action_run_scenario_loan_prime_approved", useUnmergedTree = true).performClick()
        waitForDecision("instant_personal_loan")
        waitForRoute("loan_offer")
        waitForTag("decision_summary")
        composeRule.onNodeWithTag("action_flush_pending_callbacks", useUnmergedTree = true).performScrollTo()
        waitForTag("action_flush_pending_callbacks")
        composeRule.onNodeWithTag("action_flush_pending_callbacks", useUnmergedTree = true).performClick()
        composeRule.waitUntil(15_000) {
            composeRule.onAllNodesWithTag("action_flush_pending_callbacks", useUnmergedTree = true).fetchSemanticsNodes().isEmpty()
        }
    }

    @Test
    fun liveAdminSmokeWorksAgainstBackendWhenAvailable() {
        resetAppState()

        val args = InstrumentationRegistry.getArguments()
        val backendUrl = args.getString("backendUrl") ?: "http://10.0.2.2:8080"
        val adminEmail = args.getString("adminEmail") ?: "admin@rulemind.local"
        val adminPassword = args.getString("adminPassword") ?: "rulemind-admin"
        assumeTrue("Backend is not reachable for live smoke coverage", backendReachable(backendUrl))

        replaceText("input_server_url", backendUrl)
        replaceText("input_email", adminEmail)
        replaceText("input_password", adminPassword)
        // Dismiss keyboard to ensure the connect button is visible and clickable
        androidx.test.espresso.Espresso.closeSoftKeyboard()
        composeRule.waitForIdle()
        composeRule.onNodeWithTag("action_connect_live", useUnmergedTree = true).performScrollTo()
        composeRule.onNodeWithTag("action_connect_live", useUnmergedTree = true).performClick()

        waitForLiveSession(timeoutMs = 60_000)
        waitForTag("route_experience_selection", timeoutMs = 60_000)
        composeRule.onNodeWithTag("action_open_home_dashboard", useUnmergedTree = true).performClick()
        waitForTag("route_home_dashboard")
        composeRule.onNodeWithTag("nav_admin_console", useUnmergedTree = true).performClick()
        waitForTag("route_admin_console")
        composeRule.onNodeWithTag("admin_entity_rules", useUnmergedTree = true).performScrollTo()
        composeRule.onNodeWithTag("admin_entity_rules", useUnmergedTree = true).assertIsDisplayed().performClick()
        waitForTag("action_admin_refresh")
        composeRule.onNodeWithTag("action_admin_refresh", useUnmergedTree = true).performScrollTo()
        composeRule.onNodeWithTag("nav_logs_explainability", useUnmergedTree = true).performClick()
        waitForTag("route_logs_explainability")
        composeRule.onNodeWithTag("status_connection", useUnmergedTree = true).assertIsDisplayed()
    }

    private fun resetAppState() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        context.getSharedPreferences("rulemind.sample.studio", Context.MODE_PRIVATE).edit().clear().commit()
        composeRule.activityRule.scenario.recreate()
        composeRule.waitForIdle()
    }

    private fun replaceText(tag: String, value: String) {
        composeRule.onNodeWithTag(tag, useUnmergedTree = true).performTextClearance()
        composeRule.onNodeWithTag(tag, useUnmergedTree = true).performTextInput(value)
    }

    private fun waitForTag(tag: String, timeoutMs: Long = 30_000) {
        try {
            composeRule.waitUntil(timeoutMs) {
                composeRule.onAllNodesWithTag(tag, useUnmergedTree = true).fetchSemanticsNodes().isNotEmpty()
            }
        } catch (error: ComposeTimeoutException) {
            val state = currentState()
            throw AssertionError("Timed out waiting for tag=$tag; route=${state.route}; error=${state.error}; logs=${state.logs.takeLast(8)}", error)
        }
    }

    private fun waitForDecision(journeyId: String, timeoutMs: Long = 30_000) {
        try {
            composeRule.waitUntil(timeoutMs) {
                currentState().decisions[journeyId] != null || currentState().error != null
            }
        } catch (error: ComposeTimeoutException) {
            val state = currentState()
            throw AssertionError(
                "Timed out waiting for decision=$journeyId; route=${state.route}; error=${state.error}; logs=${state.logs.takeLast(8)}",
                error,
            )
        }
        val state = currentState()
        assertNotNull(
            "Decision missing for $journeyId; route=${state.route}; error=${state.error}; logs=${state.logs.takeLast(5)}",
            state.decisions[journeyId],
        )
    }

    private fun waitForRoute(route: String, timeoutMs: Long = 30_000) {
        try {
            composeRule.waitUntil(timeoutMs) {
                currentState().route == route || currentState().error != null
            }
        } catch (error: ComposeTimeoutException) {
            val state = currentState()
            throw AssertionError("Timed out waiting for route=$route; current=${state.route}; error=${state.error}; logs=${state.logs.takeLast(8)}", error)
        }
        val state = currentState()
        assertEquals("Unexpected route transition; error=${state.error}; logs=${state.logs.takeLast(5)}", route, state.route)
    }

    private fun waitForLiveSession(timeoutMs: Long = 30_000) {
        try {
            composeRule.waitUntil(timeoutMs) {
                currentState().session.mode == "live" || currentState().error != null
            }
        } catch (error: ComposeTimeoutException) {
            val state = currentState()
            throw AssertionError(
                "Timed out waiting for live session; route=${state.route}; mode=${state.session.mode}; connection=${state.session.connectionStatus}; error=${state.error}; logs=${state.logs.takeLast(8)}",
                error,
            )
        }
        val state = currentState()
        assertEquals("Live session failed to initialize; error=${state.error}; logs=${state.logs.takeLast(5)}", "live", state.session.mode)
    }

    private fun currentState(): AppState =
        ViewModelProvider(composeRule.activity)[RuleMindViewModel::class.java].state.value

    private fun backendReachable(baseUrl: String): Boolean = runCatching {
        val healthUrl = URL("${baseUrl.trimEnd('/')}/health")
        val connection = healthUrl.openConnection() as java.net.HttpURLConnection
        connection.connectTimeout = 5_000
        connection.readTimeout = 5_000
        connection.requestMethod = "GET"
        val code = connection.responseCode
        connection.disconnect()
        code in 200..299
    }.getOrDefault(false)
}
