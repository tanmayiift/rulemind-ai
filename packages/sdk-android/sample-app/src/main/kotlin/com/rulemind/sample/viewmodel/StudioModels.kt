package com.rulemind.sample.viewmodel

import com.rulemind.core.models.Bundle
import com.rulemind.core.models.Decision

data class StudioMetric(
    val label: String,
    val value: String,
    val trend: String,
)

data class StudioField(
    val id: String,
    val label: String,
    val kind: String,
    val required: Boolean = false,
    val hint: String = "",
    val keyboard: String = "text",
    val options: List<String> = emptyList(),
    val multi: Boolean = false,
    val min: Double? = null,
    val max: Double? = null,
    val language: String? = null,
    val pattern: String? = null,
    val patternMessage: String? = null,
)

data class SandboxField(
    val key: String = "",
    val value: String = "",
    val type: String = "string",
)

data class SandboxState(
    val selectedPolicyId: String? = null,
    val fields: List<SandboxField> = listOf(SandboxField()),
    val result: Decision? = null,
    val error: String? = null,
)

data class StudioScreen(
    val id: String,
    val title: String,
    val subtitle: String,
    val type: String,
    val bullets: List<String> = emptyList(),
    val fields: List<StudioField> = emptyList(),
)

data class StudioJourney(
    val id: String,
    val title: String,
    val category: String,
    val policyId: String,
    val stepCount: Int,
    val primaryColor: Long,
    val screens: List<StudioScreen>,
)

data class StudioScenario(
    val id: String,
    val journeyId: String,
    val title: String,
    val badge: String,
    val expectedOutcome: String,
    val expectedStatus: String,
    val payload: Map<String, Any?>,
)

data class StudioAdminTool(
    val id: String,
    val title: String,
    val description: String,
)

data class StudioEntitySchema(
    val id: String,
    val title: String,
    val listPath: String,
    val detailPath: String,
    val createPath: String? = null,
    val updatePath: String? = null,
    val testPath: String? = null,
    val draftTestPath: String? = null,
    val supportsCreate: Boolean = false,
    val supportsDelete: Boolean = false,
    val supportsPromote: Boolean = false,
    val fields: List<StudioField> = emptyList(),
)

data class StudioShellSection(
    val id: String,
    val title: String,
    val icon: String,
)

data class StudioContent(
    val appName: String,
    val tagline: String,
    val version: String,
    val sections: List<StudioShellSection>,
    val journeys: List<StudioJourney>,
    val scenarios: List<StudioScenario>,
    val adminTools: List<StudioAdminTool>,
    val adminEntities: List<StudioEntitySchema>,
    val logMetrics: List<StudioMetric>,
)

data class StudioTenant(
    val id: String,
    val name: String,
    val plan: String? = null,
)

data class StudioSession(
    val mode: String? = null,
    val baseUrl: String = "http://10.0.2.2:8080",
    val email: String = "",
    val password: String = "",
    val accessToken: String? = null,
    val apiKey: String = "",
    val tenantId: String? = null,
    val tenantName: String? = null,
    val userEmail: String? = null,
    val connectionStatus: String? = null,
    val latestBundleVersion: Int = 0,
    val availableTenants: List<StudioTenant> = emptyList(),
)

data class LogEntry(
    val timestamp: String,
    val message: String,
    val isError: Boolean = false,
)

data class ExecutionSummary(
    val journeyId: String,
    val executionId: String?,
    val outcome: String,
    val status: String,
    val score: Double?,
    val source: String?,
    val timestamp: String,
)

data class AdminRecord(
    val id: String,
    val title: String,
    val raw: Map<String, Any?>,
)

data class AdminState(
    val selectedEntityId: String? = null,
    val selectedRecordId: String? = null,
    val isCreating: Boolean = false,
    val records: Map<String, List<AdminRecord>> = emptyMap(),
    val editorValues: Map<String, Any?> = emptyMap(),
    val editorRawValues: Map<String, String> = emptyMap(),
    val status: String? = null,
)

data class AppState(
    val isLoading: Boolean = false,
    val route: String = "access",
    val content: StudioContent = StudioFixtures.demoContent(),
    val session: StudioSession = StudioSession(),
    val bundle: Bundle? = null,
    val lastSync: String? = null,
    val currentJourneyId: String? = null,
    val drafts: Map<String, Map<String, Any?>> = emptyMap(),
    val decisions: Map<String, Decision> = emptyMap(),
    val history: List<ExecutionSummary> = emptyList(),
    val logs: List<LogEntry> = emptyList(),
    val admin: AdminState = AdminState(),
    val error: String? = null,
    /** Field validation errors: journeyId → fieldId → error message (null = valid) */
    val fieldErrors: Map<String, Map<String, String?>> = emptyMap(),
    /** Login form validation errors: "email" | "url" → error message */
    val loginErrors: Map<String, String?> = emptyMap(),
    /** Dynamic offline sandbox state */
    val sandbox: SandboxState = SandboxState(),
)
