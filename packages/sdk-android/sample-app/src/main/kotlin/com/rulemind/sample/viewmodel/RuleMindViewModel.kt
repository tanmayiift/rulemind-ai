package com.rulemind.sample.viewmodel

import android.content.Context
import android.content.Context.MODE_PRIVATE
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.rulemind.android.RuleMind
import com.rulemind.core.RuleMindEngine
import com.rulemind.core.models.Decision
import com.rulemind.core.models.ReviewDecision
import com.rulemind.core.models.RuleMindConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.BufferedWriter
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class RuleMindViewModel : ViewModel() {
    private val _state = MutableStateFlow(AppState())
    val state: StateFlow<AppState> = _state.asStateFlow()

    private val engine = RuleMindEngine()
    private val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.US)
    private val prefsName = "rulemind.sample.studio"

    fun loadSavedState(context: Context) {
        val prefs = context.getSharedPreferences(prefsName, MODE_PRIVATE)
        val savedMode = prefs.getString("preferred_mode", null)
        val baseUrl = prefs.getString("base_url", _state.value.session.baseUrl) ?: _state.value.session.baseUrl
        val email = prefs.getString("email", "") ?: ""
        val route = prefs.getString("route", "access") ?: "access"
        val journeyId = prefs.getString("journey_id", null)
        val drafts = prefs.getString("drafts_json", null)?.let { decodeDrafts(it) } ?: emptyMap()
        val baseline = AppState(
            content = StudioFixtures.demoContent(),
            route = if (savedMode == "demo" && route != "access") route else "access",
            session = StudioSession(
                mode = savedMode,
                baseUrl = baseUrl,
                email = email,
            ),
            currentJourneyId = if (savedMode == "demo") journeyId else null,
            drafts = drafts,
        )
        _state.value = baseline
        if (savedMode == "demo") {
            val content = StudioFixtures.demoContent()
            _state.value = _state.value.copy(
                content = content,
                bundle = StudioFixtures.demoBundle(),
                session = _state.value.session.copy(
                    mode = "demo",
                    tenantName = "Local Demo Studio",
                    connectionStatus = "Offline bundle ready",
                    latestBundleVersion = 7,
                ),
            )
        }
    }

    fun updateBaseUrl(value: String) {
        _state.value = _state.value.copy(session = _state.value.session.copy(baseUrl = value))
    }

    fun updateEmail(value: String) {
        _state.value = _state.value.copy(session = _state.value.session.copy(email = value))
    }

    fun updatePassword(value: String) {
        _state.value = _state.value.copy(session = _state.value.session.copy(password = value))
    }

    fun goTo(route: String, journeyId: String? = _state.value.currentJourneyId) {
        _state.value = _state.value.copy(route = route, currentJourneyId = journeyId ?: _state.value.currentJourneyId)
    }

    fun openExperienceSelection() {
        _state.value = _state.value.copy(route = "experience_selection")
    }

    fun openHome() {
        _state.value = _state.value.copy(route = "home_dashboard")
    }

    fun selectJourney(journeyId: String) {
        val journey = _state.value.content.journeys.firstOrNull { it.id == journeyId } ?: return
        _state.value = _state.value.copy(currentJourneyId = journeyId, route = journey.screens.first().id)
    }

    fun nextJourneyScreen() {
        val currentJourney = currentJourney() ?: return
        val currentIndex = currentJourney.screens.indexOfFirst { it.id == _state.value.route }
        if (currentIndex >= 0 && currentIndex + 1 < currentJourney.screens.size) {
            _state.value = _state.value.copy(route = currentJourney.screens[currentIndex + 1].id)
        }
    }

    fun previousJourneyScreen() {
        val currentJourney = currentJourney() ?: return
        val currentIndex = currentJourney.screens.indexOfFirst { it.id == _state.value.route }
        if (currentIndex > 0) {
            _state.value = _state.value.copy(route = currentJourney.screens[currentIndex - 1].id)
        } else {
            _state.value = _state.value.copy(route = "home_dashboard")
        }
    }

    fun updateDraftField(journeyId: String, field: StudioField, value: String) {
        val existing = _state.value.drafts[journeyId].orEmpty().toMutableMap()
        existing[field.id] = when (field.kind) {
            "number" -> value.toDoubleOrNull()
            "choice" -> if (field.multi) value.split(",").mapNotNull { it.trim().takeIf(String::isNotEmpty) } else value
            else -> value
        }
        _state.value = _state.value.copy(drafts = _state.value.drafts + (journeyId to existing))
    }

    suspend fun startDemo(context: Context): Boolean = withContext(Dispatchers.IO) {
        _state.value = _state.value.copy(isLoading = true, error = null)
        val content = StudioFixtures.demoContent()
        val bundle = StudioFixtures.demoBundle()
        _state.value = _state.value.copy(
            isLoading = false,
            route = "experience_selection",
            content = content,
            bundle = bundle,
            session = _state.value.session.copy(
                mode = "demo",
                accessToken = null,
                apiKey = "",
                tenantId = "demo-tenant",
                tenantName = "Local Demo Studio",
                userEmail = null,
                connectionStatus = "Offline bundle ready",
                latestBundleVersion = bundle.bundleVersion,
                password = "",
            ),
            lastSync = timeFmt.format(Date()),
        )
        log("Demo studio initialized with ${content.journeys.size} journeys and ${content.scenarios.size} scenarios.")
        persist(context)
        true
    }

    suspend fun loginLive(context: Context): Boolean = withContext(Dispatchers.IO) {
        val session = _state.value.session
        if (session.baseUrl.isBlank() || session.email.isBlank() || session.password.isBlank()) {
            _state.value = _state.value.copy(error = "Base URL, email, and password are required.")
            log("Live mode requires base URL, email, and password.", isError = true)
            return@withContext false
        }
        _state.value = _state.value.copy(isLoading = true, error = null)
        return@withContext try {
            val response = requestJson(
                baseUrl = session.baseUrl,
                path = "/api/mobile/v1/auth/login",
                method = "POST",
                body = mapOf(
                    "email" to session.email,
                    "password" to session.password,
                ),
            ) as Map<String, Any?>
            val accessToken = response["accessToken"]?.toString()
            val apiKey = response["apiKey"]?.toString().orEmpty()
            val tenant = response["tenant"] as? Map<String, Any?> ?: emptyMap()
            val manifest = response["experienceManifest"] as? Map<String, Any?>
            val content = manifest?.let(::parseContentFromManifest) ?: StudioFixtures.demoContent()
            val availableTenants = (response["availableTenants"] as? List<*>)?.mapNotNull { item ->
                (item as? Map<*, *>)?.let { StudioTenant(it["id"].toString(), it["name"].toString(), it["plan"]?.toString()) }
            }.orEmpty()
            RuleMind.initialize(
                context = context,
                config = RuleMindConfig(baseUrl = session.baseUrl, apiKey = apiKey),
            )
            val health = RuleMind.health()
            val bundle = RuleMind.syncNow()
            _state.value = _state.value.copy(
                isLoading = false,
                route = "experience_selection",
                content = content,
                session = session.copy(
                    mode = "live",
                    accessToken = accessToken,
                    apiKey = apiKey,
                    tenantId = tenant["id"]?.toString(),
                    tenantName = tenant["name"]?.toString(),
                    userEmail = (response["user"] as? Map<*, *>)?.get("email")?.toString() ?: session.email,
                    connectionStatus = "Connected • latest bundle v${bundle?.bundleVersion ?: health["latestBundleVersion"] ?: 0}",
                    latestBundleVersion = (health["latestBundleVersion"] as? Number)?.toInt() ?: (bundle?.bundleVersion ?: 0),
                    availableTenants = availableTenants,
                    password = "",
                ),
                bundle = bundle ?: _state.value.bundle,
                lastSync = timeFmt.format(Date()),
            )
            log("Live studio connected for tenant ${tenant["name"] ?: "Unknown Tenant"}.")
            log("Bundle synced to v${bundle?.bundleVersion ?: health["latestBundleVersion"] ?: 0}.")
            persist(context)
            true
        } catch (error: Exception) {
            val detail = buildString {
                append(error::class.java.simpleName)
                error.message?.takeIf { it.isNotBlank() }?.let {
                    append(": ")
                    append(it)
                }
                error.cause?.let { cause ->
                    append(" [cause=")
                    append(cause::class.java.simpleName)
                    cause.message?.takeIf { it.isNotBlank() }?.let { causeMessage ->
                        append(": ")
                        append(causeMessage)
                    }
                    append("]")
                }
            }
            _state.value = _state.value.copy(isLoading = false, error = detail)
            log("Live studio login failed: $detail", isError = true)
            false
        }
    }

    fun syncBundle(context: Context) {
        viewModelScope.launch(Dispatchers.IO) {
            if (_state.value.session.mode != "live") {
                log("Demo mode is already using the local experience bundle.")
                return@launch
            }
            _state.value = _state.value.copy(isLoading = true)
            runCatching {
                val bundle = RuleMind.syncNow()
                val latest = bundle?.bundleVersion ?: _state.value.session.latestBundleVersion
                _state.value = _state.value.copy(
                    isLoading = false,
                    bundle = bundle ?: _state.value.bundle,
                    lastSync = timeFmt.format(Date()),
                    session = _state.value.session.copy(
                        latestBundleVersion = latest,
                        connectionStatus = "Bundle synced • v$latest",
                    ),
                )
                log("Bundle synced: v$latest")
                persist(context)
            }.onFailure { error ->
                _state.value = _state.value.copy(isLoading = false, error = error.message)
                log("Bundle sync failed: ${error.message}", isError = true)
            }
        }
    }

    fun runScenarioAsync(context: Context, scenarioId: String) {
        viewModelScope.launch(Dispatchers.IO) {
            runScenario(context, scenarioId)
        }
    }

    suspend fun runScenario(context: Context, scenarioId: String): Boolean = withContext(Dispatchers.IO) {
        val scenario = _state.value.content.scenarios.firstOrNull { it.id == scenarioId } ?: return@withContext false
        val journeyId = scenario.journeyId
        val updatedDrafts = _state.value.drafts + (journeyId to flattenPayloadSection(scenario.payload[payloadKeyForJourney(journeyId)] as? Map<String, Any?>))
        _state.value = _state.value.copy(drafts = updatedDrafts)
        evaluateJourney(context, journeyId, scenario.payload)
    }

    suspend fun evaluateJourney(context: Context, journeyId: String, explicitPayload: Map<String, Any?>? = null): Boolean = withContext(Dispatchers.IO) {
        val journey = _state.value.content.journeys.firstOrNull { it.id == journeyId } ?: return@withContext false
        _state.value = _state.value.copy(isLoading = true, error = null)
        return@withContext try {
            val payload = explicitPayload ?: buildPayloadForJourney(journeyId)
            val decision = if (_state.value.session.mode == "live") {
                RuleMind.evaluate(journey.policyId, payload, userId = "studio_mobile_user")
            } else {
                val bundle = _state.value.bundle ?: StudioFixtures.demoBundle()
                engine.evaluate(bundle, journey.policyId, payload, userId = "demo_user")
                    .copy(source = "demo_bundle", policyId = journey.policyId, userId = "demo_user")
            }
            applyDecision(context, journeyId, decision)
            val outcomeScreen = journey.screens.firstOrNull { it.type == "outcome" }?.id ?: journey.screens.last().id
            _state.value = _state.value.copy(isLoading = false, route = outcomeScreen, currentJourneyId = journeyId)
            true
        } catch (error: Exception) {
            _state.value = _state.value.copy(isLoading = false, error = error.message)
            log("Journey evaluation failed: ${error.message}", isError = true)
            false
        }
    }

    fun flushPendingOperations(context: Context, journeyId: String) {
        viewModelScope.launch(Dispatchers.IO) {
            val decision = _state.value.decisions[journeyId] ?: return@launch
            val updated = if (_state.value.session.mode == "live") {
                RuleMind.flushPendingOperations().firstOrNull { it.executionId == decision.executionId || it.policyId == decision.policyId } ?: decision
            } else {
                flushDemoDecision(decision)
            }
            applyDecision(context, journeyId, updated)
            log("Flushed pending callbacks for ${journeyTitle(journeyId)}.")
        }
    }

    fun resumeReview(context: Context, journeyId: String, approve: Boolean, notes: String = "") {
        viewModelScope.launch(Dispatchers.IO) {
            val decision = _state.value.decisions[journeyId] ?: return@launch
            val reviewDecision = ReviewDecision(
                decision = if (approve) "approve" else "reject",
                reviewerId = _state.value.session.userEmail ?: "studio_reviewer",
                response = mapOf("notes" to notes, "decision_label" to if (approve) "Approved" else "Rejected"),
            )
            runCatching {
                val updated = if (_state.value.session.mode == "live" && !decision.executionId.isNullOrBlank()) {
                    RuleMind.resumeExecution(decision.executionId!!, reviewDecision)
                } else {
                    val bundle = _state.value.bundle ?: StudioFixtures.demoBundle()
                    engine.evaluate(
                        bundle,
                        decision.policyId ?: currentJourney()?.policyId ?: return@runCatching decision,
                        decision.payload,
                        userId = decision.userId,
                        resumeFrom = decision.copy(
                            status = "running",
                            pausedAtStep = null,
                            currentStepIndex = (decision.pausedAtStep ?: decision.currentStepIndex) + 1,
                            reviewTask = (decision.reviewTask ?: emptyMap()) + mapOf(
                                "status" to if (approve) "approved" else "rejected",
                                "reviewed_by" to (_state.value.session.userEmail ?: "studio_reviewer"),
                            ),
                            reviewResponse = decision.reviewResponse + reviewDecision.response,
                            outcome = if (approve) "approve" else "reject",
                        ),
                    ).copy(source = "demo_resume", policyId = decision.policyId, userId = decision.userId)
                }
                applyDecision(context, journeyId, updated)
                _state.value = _state.value.copy(route = journeyById(journeyId)?.screens?.lastOrNull()?.id ?: _state.value.route)
                log("Review completed for ${journeyTitle(journeyId)}.")
            }.onFailure { error ->
                _state.value = _state.value.copy(error = error.message)
                log("Review resume failed: ${error.message}", isError = true)
            }
        }
    }

    fun selectAdminEntity(entityId: String) {
        _state.value = _state.value.copy(admin = _state.value.admin.copy(selectedEntityId = entityId, selectedRecordId = null, isCreating = false))
    }

    fun loadAdminRecords(entityId: String) {
        viewModelScope.launch(Dispatchers.IO) {
            val schema = _state.value.content.adminEntities.firstOrNull { it.id == entityId } ?: return@launch
            if (_state.value.session.mode != "live") {
                _state.value = _state.value.copy(admin = _state.value.admin.copy(status = "Live admin sign-in unlocks authoring and save/delete actions."))
                return@launch
            }
            _state.value = _state.value.copy(isLoading = true)
            runCatching {
                val response = requestJson(
                    baseUrl = _state.value.session.baseUrl,
                    path = schema.listPath,
                    method = "GET",
                    apiKey = _state.value.session.apiKey,
                )
                val records = when (response) {
                    is List<*> -> response.mapNotNull { toAdminRecord(it as? Map<String, Any?>) }
                    is Map<*, *> -> listOf(toAdminRecord(response as Map<String, Any?>, fallbackId = "settings")).filterNotNull()
                    else -> emptyList()
                }
                _state.value = _state.value.copy(
                    isLoading = false,
                    admin = _state.value.admin.copy(
                        selectedEntityId = entityId,
                        records = _state.value.admin.records + (entityId to records),
                        status = "Loaded ${records.size} ${schema.title.lowercase(Locale.US)}.",
                    ),
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(isLoading = false, error = error.message)
                log("Admin list load failed: ${error.message}", isError = true)
            }
        }
    }

    fun startAdminCreate() {
        val entityId = _state.value.admin.selectedEntityId ?: return
        val schema = _state.value.content.adminEntities.firstOrNull { it.id == entityId } ?: return
        _state.value = _state.value.copy(
            admin = _state.value.admin.copy(
                isCreating = true,
                selectedRecordId = null,
                editorValues = emptyMap(),
                editorRawValues = schema.fields.associate { it.id to "" },
            )
        )
    }

    fun editAdminRecord(record: AdminRecord) {
        val entityId = _state.value.admin.selectedEntityId ?: return
        val schema = _state.value.content.adminEntities.firstOrNull { it.id == entityId } ?: return
        _state.value = _state.value.copy(
            admin = _state.value.admin.copy(
                isCreating = false,
                selectedRecordId = record.id,
                editorValues = record.raw,
                editorRawValues = schema.fields.associate { field -> field.id to formatFieldValue(record.raw[field.id], field) },
            )
        )
    }

    fun updateAdminField(field: StudioField, rawValue: String) {
        _state.value = _state.value.copy(
            admin = _state.value.admin.copy(
                editorRawValues = _state.value.admin.editorRawValues + (field.id to rawValue),
            )
        )
    }

    fun cancelAdminEdit() {
        _state.value = _state.value.copy(admin = _state.value.admin.copy(selectedRecordId = null, isCreating = false, editorValues = emptyMap(), editorRawValues = emptyMap()))
    }

    fun saveAdminRecord() {
        viewModelScope.launch(Dispatchers.IO) {
            val entityId = _state.value.admin.selectedEntityId ?: return@launch
            val schema = _state.value.content.adminEntities.firstOrNull { it.id == entityId } ?: return@launch
            if (_state.value.session.mode != "live") {
                _state.value = _state.value.copy(admin = _state.value.admin.copy(status = "Demo mode is read-only for admin authoring."))
                return@launch
            }
            val payload = buildAdminPayload(schema, _state.value.admin.editorRawValues)
            val isCreate = _state.value.admin.isCreating || _state.value.admin.selectedRecordId == null
            val path = when {
                entityId == "settings" -> schema.updatePath ?: schema.detailPath
                isCreate -> schema.createPath
                else -> interpolatePath(schema.updatePath ?: schema.detailPath, _state.value.admin.selectedRecordId)
            } ?: return@launch
            val method = if (isCreate && entityId != "settings") "POST" else "PUT"
            _state.value = _state.value.copy(isLoading = true)
            runCatching {
                requestJson(
                    baseUrl = _state.value.session.baseUrl,
                    path = path,
                    method = method,
                    body = payload,
                    apiKey = _state.value.session.apiKey,
                )
                _state.value = _state.value.copy(
                    isLoading = false,
                    admin = _state.value.admin.copy(
                        status = "${schema.title.removeSuffix("s")} saved successfully.",
                        selectedRecordId = null,
                        isCreating = false,
                        editorValues = emptyMap(),
                        editorRawValues = emptyMap(),
                    ),
                )
                loadAdminRecords(entityId)
                log("Saved ${schema.title.removeSuffix("s").lowercase(Locale.US)}.")
            }.onFailure { error ->
                _state.value = _state.value.copy(isLoading = false, error = error.message)
                log("Admin save failed: ${error.message}", isError = true)
            }
        }
    }

    fun deleteAdminRecord(recordId: String) {
        viewModelScope.launch(Dispatchers.IO) {
            val entityId = _state.value.admin.selectedEntityId ?: return@launch
            val schema = _state.value.content.adminEntities.firstOrNull { it.id == entityId } ?: return@launch
            if (_state.value.session.mode != "live") {
                _state.value = _state.value.copy(admin = _state.value.admin.copy(status = "Demo mode cannot delete assets."))
                return@launch
            }
            val path = interpolatePath(schema.detailPath, recordId)
            _state.value = _state.value.copy(isLoading = true)
            runCatching {
                requestJson(
                    baseUrl = _state.value.session.baseUrl,
                    path = path,
                    method = "DELETE",
                    apiKey = _state.value.session.apiKey,
                )
                _state.value = _state.value.copy(isLoading = false, admin = _state.value.admin.copy(status = "${schema.title.removeSuffix("s")} deleted."))
                loadAdminRecords(entityId)
                log("Deleted ${schema.title.removeSuffix("s").lowercase(Locale.US)}.")
            }.onFailure { error ->
                _state.value = _state.value.copy(isLoading = false, error = error.message)
                log("Admin delete failed: ${error.message}", isError = true)
            }
        }
    }

    fun switchTenant(context: Context, tenantId: String) {
        viewModelScope.launch(Dispatchers.IO) {
            val token = _state.value.session.accessToken ?: return@launch
            runCatching {
                val response = requestJson(
                    baseUrl = _state.value.session.baseUrl,
                    path = "/api/mobile/v1/tenants/$tenantId/session",
                    method = "POST",
                    bearerToken = token,
                ) as Map<String, Any?>
                val tenant = response["tenant"] as? Map<String, Any?> ?: emptyMap()
                _state.value = _state.value.copy(
                    session = _state.value.session.copy(
                        apiKey = response["apiKey"]?.toString().orEmpty(),
                        tenantId = tenant["id"]?.toString(),
                        tenantName = tenant["name"]?.toString(),
                    )
                )
                log("Switched admin context to ${tenant["name"] ?: "selected tenant"}.")
                persist(context)
            }.onFailure { error ->
                _state.value = _state.value.copy(error = error.message)
                log("Tenant switch failed: ${error.message}", isError = true)
            }
        }
    }

    fun logout(context: Context) {
        _state.value = AppState(
            content = StudioFixtures.demoContent(),
            route = "access",
            session = StudioSession(baseUrl = _state.value.session.baseUrl, email = _state.value.session.email),
            drafts = _state.value.drafts,
        )
        persist(context)
    }

    private fun applyDecision(context: Context, journeyId: String, decision: Decision) {
        val summary = ExecutionSummary(
            journeyId = journeyId,
            executionId = decision.executionId,
            outcome = decision.outcome,
            status = decision.status,
            score = decision.score,
            source = decision.source,
            timestamp = timeFmt.format(Date()),
        )
        _state.value = _state.value.copy(
            decisions = _state.value.decisions + (journeyId to decision),
            history = listOf(summary) + _state.value.history.filterNot { it.executionId == summary.executionId }.take(19),
            lastSync = _state.value.lastSync ?: timeFmt.format(Date()),
        )
        log(
            "${journeyTitle(journeyId)}: ${decision.outcome.uppercase(Locale.US)} • ${decision.status} • score ${formatNumber(decision.score)} • ${decision.latencyMs}ms"
        )
        if (decision.pendingOperations.isNotEmpty()) {
            log("${journeyTitle(journeyId)} queued ${decision.pendingOperations.count { it["status"] != "delivered" }} callback operation(s).")
        }
        if (decision.reviewTask != null && decision.status == "paused") {
            log("${journeyTitle(journeyId)} is waiting for manual review.")
        }
        persist(context)
    }

    private fun currentJourney(): StudioJourney? = journeyById(_state.value.currentJourneyId)

    private fun journeyById(journeyId: String?): StudioJourney? = _state.value.content.journeys.firstOrNull { it.id == journeyId }

    private fun journeyTitle(journeyId: String): String = journeyById(journeyId)?.title ?: journeyId

    private fun buildPayloadForJourney(journeyId: String): Map<String, Any?> {
        val draft = _state.value.drafts[journeyId].orEmpty()
        return when (journeyId) {
            "travel_guard" -> mapOf("travel" to buildTravelPayload(draft))
            "instant_personal_loan" -> mapOf("loan" to buildLoanPayload(draft))
            "sme_underwriting" -> mapOf("sme" to buildSmePayload(draft))
            else -> emptyMap()
        }
    }

    private fun buildTravelPayload(draft: Map<String, Any?>): Map<String, Any?> {
        val country = draft["destination_country"]?.toString().orEmpty()
        val medicalFlag = draft["medical_flag"]?.toString() == "Additional coverage required"
        val visaReady = if (draft["visa_status"]?.toString() == "Visa held or not required") 1 else 0
        val destinationRisk = when (country) {
            "Japan", "United Arab Emirates" -> 2
            "France", "Portugal", "Switzerland" -> 1
            else -> 3
        }
        return draft.toMutableMap().apply {
            put("visa_ready", visaReady)
            put("destination_risk", destinationRisk)
            put("support_matrix_ready", if (country.isBlank()) 0 else 1)
            put("medical_flag_value", if (medicalFlag) 1 else 0)
        }
    }

    private fun buildLoanPayload(draft: Map<String, Any?>): Map<String, Any?> {
        val monthlyIncome = numeric(draft["monthly_income_inr"])
        val existingEmi = numeric(draft["existing_emi_inr"])
        val fixedExpenses = numeric(draft["fixed_expenses_inr"])
        val variableExpenses = numeric(draft["variable_expenses_inr"])
        val housingCost = numeric(draft["housing_cost_inr"])
        val workYears = numeric(draft["work_experience_years"])
        val employerTenure = numeric(draft["employer_tenure_months"])
        val salaryVintage = numeric(draft["salary_account_vintage_months"])
        val liveness = numeric(draft["liveness_score"])
        val panVerified = draft["pan_verified"]?.toString() == "Authenticated"
        val geoMatched = draft["geo_consistency"]?.toString() == "Matched"
        val utilityVerified = draft["utility_bill_verified"]?.toString() == "Yes, Verified"
        val employmentType = draft["employment_type"]?.toString()
        var bureauScore = 620.0
        bureauScore += if (employmentType == "Salaried") 35 else 15
        bureauScore += (workYears * 8.0).coerceAtMost(60.0)
        bureauScore += if (employerTenure >= 24) 18 else if (employerTenure >= 12) 8 else -12
        bureauScore += if (salaryVintage >= 24) 20 else if (salaryVintage >= 12) 8 else -10
        bureauScore += if (utilityVerified) 10 else -25
        bureauScore += if (panVerified) 12 else -85
        bureauScore += if (geoMatched) 6 else -42
        bureauScore += if (liveness >= 95) 18 else if (liveness >= 90) 6 else -35
        bureauScore = bureauScore.coerceIn(300.0, 850.0)
        val dtiRatio = if (monthlyIncome <= 0.0) 0.0 else ((existingEmi + housingCost + (fixedExpenses * 0.3)) / monthlyIncome).coerceIn(0.0, 1.0)
        val avgBalance = (monthlyIncome * 1.35 - (fixedExpenses + variableExpenses + existingEmi + housingCost * 0.5)).coerceAtLeast(5000.0)
        return draft.toMutableMap().apply {
            put("bureau_score", draft["bureau_score"] ?: bureauScore.toInt())
            put("avg_balance_inr", draft["avg_balance_inr"] ?: avgBalance.toInt())
            put("dti_ratio", draft["dti_ratio"] ?: roundTo3(dtiRatio))
            put("pan_verified_flag", if (panVerified) 1 else 0)
            put("geo_consistency_flag", if (geoMatched) 0 else 1)
        }
    }

    private fun buildSmePayload(draft: Map<String, Any?>): Map<String, Any?> {
        val years = numeric(draft["years_in_business"]).coerceAtLeast(1.0)
        val employeeCount = numeric(draft["employee_count"])
        val uploadedScore = listOf("coi_uploaded", "pan_uploaded", "claims_report_uploaded", "signed_declaration_uploaded")
            .count { draft[it]?.toString() == "Uploaded" }
        val gstCompliance = (55 + uploadedScore * 9 + years * 2).coerceIn(40.0, 98.0)
        val growthRate = ((employeeCount / years) * 1.2).coerceIn(8.0, 85.0)
        return draft.toMutableMap().apply {
            put("gst_compliance_score", draft["gst_compliance_score"] ?: gstCompliance.toInt())
            put("growth_rate_pct", draft["growth_rate_pct"] ?: roundTo3(growthRate))
            put("ubo_docs_ready_flag", if (draft["ubo_docs_ready"]?.toString() == "Ready") 1 else 0)
            put("claims_disclosed_flag", if (draft["claims_disclosed"]?.toString() == "Yes") 1 else 0)
            put("sanctions_flag", draft["sanctions_flag"] ?: 0)
        }
    }

    private fun flushDemoDecision(decision: Decision): Decision {
        val actionResults = decision.actionResults.map { it.toMutableMap() }.toMutableList()
        val pending = decision.pendingOperations.map { operation ->
            val delivered = operation.toMutableMap().apply {
                this["status"] = "delivered"
                this["lastResult"] = mapOf(
                    "operationId" to operation["id"],
                    "stepId" to operation["stepId"],
                    "status" to "delivered",
                    "responseStatus" to 202,
                    "responseBody" to """{"simulated":true}""",
                    "success" to true,
                )
            }
            val actionIndex = actionResults.indexOfFirst { it["operationId"] == operation["id"] }
            if (actionIndex >= 0) {
                actionResults[actionIndex]["status"] = "delivered"
                actionResults[actionIndex]["responseStatus"] = 202
                actionResults[actionIndex]["success"] = true
                actionResults[actionIndex]["responseBody"] = """{"simulated":true}"""
            }
            delivered
        }
        return decision.copy(
            pendingOperations = pending,
            actionResults = actionResults,
            auditSummary = decision.auditSummary + mapOf("pendingOperationCount" to 0),
        )
    }

    private fun persist(context: Context) {
        val prefs = context.getSharedPreferences(prefsName, MODE_PRIVATE)
        prefs.edit()
            .putString("preferred_mode", _state.value.session.mode)
            .putString("base_url", _state.value.session.baseUrl)
            .putString("email", _state.value.session.email)
            .putString("route", _state.value.route)
            .putString("journey_id", _state.value.currentJourneyId)
            .putString("drafts_json", encodeDrafts(_state.value.drafts))
            .apply()
    }

    private fun encodeDrafts(drafts: Map<String, Map<String, Any?>>): String {
        val root = JSONObject()
        drafts.forEach { (journeyId, fields) ->
            root.put(journeyId, mapToJson(fields))
        }
        return root.toString()
    }

    private fun decodeDrafts(raw: String): Map<String, Map<String, Any?>> {
        val root = JSONObject(raw)
        val result = mutableMapOf<String, Map<String, Any?>>()
        root.keys().forEach { key ->
            result[key] = root.optJSONObject(key)?.let(::jsonObjectToMap) ?: emptyMap()
        }
        return result
    }

    private fun buildAdminPayload(schema: StudioEntitySchema, rawValues: Map<String, String>): Map<String, Any?> {
        val payload = linkedMapOf<String, Any?>()
        schema.fields.forEach { field ->
            val raw = rawValues[field.id]?.trim().orEmpty()
            if (raw.isEmpty() && !field.required && field.kind != "boolean") {
                return@forEach
            }
            payload[field.id] = parseFieldValue(field, raw)
        }
        return payload
    }

    private fun parseFieldValue(field: StudioField, raw: String): Any? = when (field.kind) {
        "number" -> raw.toDoubleOrNull()?.let { if (raw.contains(".")) it else it.toInt() } ?: 0
        "boolean" -> raw.equals("true", ignoreCase = true)
        "choice" -> if (field.multi) raw.split(",").mapNotNull { it.trim().takeIf(String::isNotEmpty) } else raw
        "json" -> if (raw.startsWith("[")) jsonArrayToList(JSONArray(raw)) else jsonObjectToMap(JSONObject(raw))
        else -> raw
    }

    private fun formatFieldValue(value: Any?, field: StudioField): String = when (value) {
        null -> if (field.kind == "boolean") "false" else ""
        is String -> value
        is Number, is Boolean -> value.toString()
        is Map<*, *> -> mapToJson(value as Map<String, Any?>).toString(2)
        is List<*> -> listToJson(value as List<Any?>).toString(2)
        else -> value.toString()
    }

    private fun parseContentFromManifest(manifest: Map<String, Any?>): StudioContent {
        val shell = manifest["shell"] as? Map<String, Any?> ?: return StudioFixtures.demoContent()
        val sections = (shell["sections"] as? List<*>)?.mapNotNull { item ->
            (item as? Map<*, *>)?.let { StudioShellSection(it["id"].toString(), it["title"].toString(), it["icon"].toString()) }
        }.orEmpty()
        val journeys = (manifest["journeys"] as? List<*>)?.mapNotNull { item ->
            val map = item as? Map<String, Any?> ?: return@mapNotNull null
            StudioJourney(
                id = map["id"]?.toString().orEmpty(),
                title = map["title"]?.toString().orEmpty(),
                category = map["category"]?.toString().orEmpty(),
                policyId = map["policyId"]?.toString().orEmpty(),
                stepCount = (map["stepCount"] as? Number)?.toInt() ?: 0,
                primaryColor = parseColor(map["primaryColor"]?.toString()) ?: 0xFF496FBEL,
                screens = (map["screens"] as? List<*>)?.mapNotNull { screen ->
                    val screenMap = screen as? Map<String, Any?> ?: return@mapNotNull null
                    StudioScreen(
                        id = screenMap["id"]?.toString().orEmpty(),
                        title = screenMap["title"]?.toString().orEmpty(),
                        subtitle = screenMap["subtitle"]?.toString().orEmpty(),
                        type = screenMap["type"]?.toString().orEmpty(),
                        bullets = (screenMap["bullets"] as? List<*>)?.map { it.toString() }.orEmpty(),
                        fields = (screenMap["fields"] as? List<*>)?.mapNotNull { field ->
                            parseField(field as? Map<String, Any?>)
                        }.orEmpty(),
                    )
                }.orEmpty(),
            )
        }.orEmpty()
        val scenarios = (manifest["scenarios"] as? List<*>)?.mapNotNull { item ->
            val map = item as? Map<String, Any?> ?: return@mapNotNull null
            StudioScenario(
                id = map["id"]?.toString().orEmpty(),
                journeyId = map["journeyId"]?.toString().orEmpty(),
                title = map["title"]?.toString().orEmpty(),
                badge = map["badge"]?.toString().orEmpty(),
                expectedOutcome = map["expectedOutcome"]?.toString().orEmpty(),
                expectedStatus = map["expectedStatus"]?.toString().orEmpty(),
                payload = map["payload"] as? Map<String, Any?> ?: emptyMap(),
            )
        }.orEmpty()
        val admin = manifest["admin"] as? Map<String, Any?> ?: emptyMap()
        val adminTools = (admin["tools"] as? List<*>)?.mapNotNull { item ->
            val map = item as? Map<String, Any?> ?: return@mapNotNull null
            StudioAdminTool(map["id"].toString(), map["title"].toString(), map["description"].toString())
        }.orEmpty()
        val adminEntities = (admin["entities"] as? List<*>)?.mapNotNull { item ->
            parseEntitySchema(item as? Map<String, Any?>)
        }.orEmpty()
        val logMetrics = ((manifest["logsLanding"] as? Map<String, Any?>)?.get("metrics") as? List<*>)?.mapNotNull { item ->
            val map = item as? Map<String, Any?> ?: return@mapNotNull null
            StudioMetric(map["label"].toString(), map["value"].toString(), map["trend"].toString())
        }.orEmpty()
        return StudioContent(
            appName = shell["appName"]?.toString() ?: "RuleMind Experience Studio",
            tagline = shell["tagline"]?.toString() ?: StudioFixtures.demoContent().tagline,
            version = manifest["version"]?.toString() ?: StudioFixtures.demoContent().version,
            sections = if (sections.isEmpty()) StudioFixtures.demoContent().sections else sections,
            journeys = if (journeys.isEmpty()) StudioFixtures.demoContent().journeys else journeys,
            scenarios = if (scenarios.isEmpty()) StudioFixtures.demoContent().scenarios else scenarios,
            adminTools = if (adminTools.isEmpty()) StudioFixtures.demoContent().adminTools else adminTools,
            adminEntities = if (adminEntities.isEmpty()) StudioFixtures.demoContent().adminEntities else adminEntities,
            logMetrics = if (logMetrics.isEmpty()) StudioFixtures.demoContent().logMetrics else logMetrics,
        )
    }

    private fun parseField(field: Map<String, Any?>?): StudioField? {
        if (field == null) return null
        return StudioField(
            id = field["id"]?.toString().orEmpty(),
            label = field["label"]?.toString().orEmpty(),
            kind = field["kind"]?.toString().orEmpty(),
            required = field["required"] as? Boolean ?: false,
            hint = field["hint"]?.toString().orEmpty(),
            keyboard = field["keyboard"]?.toString() ?: "text",
            options = (field["options"] as? List<*>)?.map { it.toString() }.orEmpty(),
            multi = field["multi"] as? Boolean ?: false,
            min = (field["min"] as? Number)?.toDouble(),
            max = (field["max"] as? Number)?.toDouble(),
            language = field["language"]?.toString(),
        )
    }

    private fun parseEntitySchema(map: Map<String, Any?>?): StudioEntitySchema? {
        if (map == null) return null
        return StudioEntitySchema(
            id = map["id"]?.toString().orEmpty(),
            title = map["title"]?.toString().orEmpty(),
            listPath = map["listPath"]?.toString().orEmpty(),
            detailPath = map["detailPath"]?.toString().orEmpty(),
            createPath = map["createPath"]?.toString(),
            updatePath = map["updatePath"]?.toString(),
            testPath = map["testPath"]?.toString(),
            draftTestPath = map["draftTestPath"]?.toString(),
            supportsCreate = map["supportsCreate"] as? Boolean ?: false,
            supportsDelete = map["supportsDelete"] as? Boolean ?: false,
            supportsPromote = map["supportsPromote"] as? Boolean ?: false,
            fields = (map["fields"] as? List<*>)?.mapNotNull { parseField(it as? Map<String, Any?>) }.orEmpty(),
        )
    }

    private fun toAdminRecord(map: Map<String, Any?>?, fallbackId: String? = null): AdminRecord? {
        if (map == null) return null
        val id = map["id"]?.toString() ?: fallbackId ?: return null
        val title = map["name"]?.toString()
            ?: map["title"]?.toString()
            ?: map["policy_id"]?.toString()
            ?: id
        return AdminRecord(id = id, title = title, raw = map)
    }

    private fun interpolatePath(template: String, id: String?): String = template.replace("{id}", id.orEmpty())

    private fun log(message: String, isError: Boolean = false) {
        if (isError) {
            android.util.Log.e("RuleMindSample", message)
        } else {
            android.util.Log.i("RuleMindSample", message)
        }
        val entry = LogEntry(timeFmt.format(Date()), message, isError)
        _state.value = _state.value.copy(logs = (_state.value.logs + entry).takeLast(80))
    }

    private fun requestJson(
        baseUrl: String,
        path: String,
        method: String,
        body: Map<String, Any?>? = null,
        apiKey: String? = null,
        bearerToken: String? = null,
    ): Any? {
        val url = URL(baseUrl.trimEnd('/') + path)
        val connection = (url.openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 7_000
            readTimeout = 12_000
            doInput = true
            setRequestProperty("Accept", "application/json")
            if (!apiKey.isNullOrBlank()) {
                setRequestProperty("X-API-Key", apiKey)
                setRequestProperty("X-SDK-Version", "4.1.0")
            }
            if (!bearerToken.isNullOrBlank()) {
                setRequestProperty("Authorization", "Bearer $bearerToken")
            }
            if (body != null) {
                doOutput = true
                setRequestProperty("Content-Type", "application/json")
            }
        }
        if (body != null) {
            connection.outputStream.bufferedWriter().use { writer ->
                writer.writeJson(body)
            }
        }
        val status = connection.responseCode
        val raw = runCatching {
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            stream?.bufferedReader()?.use(BufferedReader::readText).orEmpty()
        }.getOrDefault("")
        if (status !in 200..299) {
            val detail = raw.takeIf { it.isNotBlank() } ?: "HTTP $status"
            throw IllegalStateException(detail)
        }
        if (raw.isBlank()) {
            return null
        }
        return when (raw.trim().firstOrNull()) {
            '[' -> jsonArrayToList(JSONArray(raw))
            '{' -> jsonObjectToMap(JSONObject(raw))
            else -> raw
        }
    }

    private fun parseColor(value: String?): Long? {
        if (value.isNullOrBlank()) return null
        return runCatching { value.removePrefix("#").toLong(16) or 0xFF000000L }.getOrNull()
    }

    private fun numeric(value: Any?): Double = when (value) {
        is Number -> value.toDouble()
        is String -> value.toDoubleOrNull() ?: 0.0
        else -> 0.0
    }

    private fun roundTo3(value: Double): Double = kotlin.math.round(value * 1000.0) / 1000.0

    private fun formatNumber(value: Double?): String = if (value == null) "—" else {
        val rounded = roundTo3(value)
        if (rounded % 1.0 == 0.0) rounded.toInt().toString() else rounded.toString()
    }

    private fun flattenPayloadSection(section: Map<String, Any?>?): Map<String, Any?> = section ?: emptyMap()

    private fun payloadKeyForJourney(journeyId: String): String = when (journeyId) {
        "travel_guard" -> "travel"
        "instant_personal_loan" -> "loan"
        "sme_underwriting" -> "sme"
        else -> journeyId
    }

    private fun mapToJson(map: Map<String, Any?>): JSONObject = JSONObject().apply {
        map.forEach { (key, value) -> put(key, anyToJson(value)) }
    }

    private fun listToJson(list: List<Any?>): JSONArray = JSONArray().apply {
        list.forEach { put(anyToJson(it)) }
    }

    private fun anyToJson(value: Any?): Any? = when (value) {
        null -> JSONObject.NULL
        is Map<*, *> -> mapToJson(value.entries.associate { it.key.toString() to it.value })
        is List<*> -> listToJson(value.map { it })
        else -> value
    }

    private fun jsonObjectToMap(json: JSONObject): Map<String, Any?> {
        val result = linkedMapOf<String, Any?>()
        json.keys().forEach { key ->
            result[key] = when (val value = json.opt(key)) {
                JSONObject.NULL -> null
                is JSONObject -> jsonObjectToMap(value)
                is JSONArray -> jsonArrayToList(value)
                else -> value
            }
        }
        return result
    }

    private fun jsonArrayToList(json: JSONArray): List<Any?> = (0 until json.length()).map { index ->
        when (val value = json.opt(index)) {
            JSONObject.NULL -> null
            is JSONObject -> jsonObjectToMap(value)
            is JSONArray -> jsonArrayToList(value)
            else -> value
        }
    }

    private fun BufferedWriter.writeJson(body: Map<String, Any?>) {
        write(mapToJson(body).toString())
        flush()
    }
}
