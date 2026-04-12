package com.rulemind.sample

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AccountBalance
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Business
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Clear
import androidx.compose.material.icons.filled.FlightTakeoff
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Logout
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Science
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Terminal
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.rulemind.core.models.Decision
import com.rulemind.sample.ui.components.OutcomeBanner
import com.rulemind.sample.ui.components.ScoreGauge
import com.rulemind.sample.ui.theme.RuleMindTheme
import com.rulemind.sample.viewmodel.AdminRecord
import com.rulemind.sample.viewmodel.AppState
import com.rulemind.sample.viewmodel.RuleMindViewModel
import com.rulemind.sample.viewmodel.StudioEntitySchema
import com.rulemind.sample.viewmodel.StudioField
import com.rulemind.sample.viewmodel.StudioJourney
import com.rulemind.sample.viewmodel.StudioScreen
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            RuleMindTheme {
                RuleMindStudioApp()
            }
        }
    }
}

@Composable
fun RuleMindStudioApp(viewModel: RuleMindViewModel = viewModel()) {
    val state by viewModel.state.collectAsState()
    val context = androidx.compose.ui.platform.LocalContext.current
    LaunchedEffect(Unit) {
        viewModel.loadSavedState(context)
    }
    Scaffold(
        topBar = {
            StudioTopBar(state = state, onBack = { handleBack(state, viewModel) }, onLogout = { viewModel.logout(context) })
        },
        bottomBar = {
            if (state.session.mode != null && state.route !in setOf("access", "experience_selection")) {
                StudioBottomBar(state = state, onNavigate = { viewModel.goTo(it, state.currentJourneyId) })
            }
        },
    ) { padding ->
        Surface(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding),
        ) {
            when (state.route) {
                "access" -> AccessScreen(viewModel)
                "experience_selection" -> ExperienceSelectionScreen(state, viewModel)
                "home_dashboard" -> HomeDashboardScreen(state, viewModel)
                "scenario_hub" -> ScenarioHubScreen(state, viewModel)
                "logs_explainability" -> LogsScreen(state, viewModel)
                "admin_console" -> AdminConsoleScreen(state, viewModel)
                "sandbox" -> SandboxScreen(state, viewModel)
                else -> JourneyScreen(state, viewModel)
            }
        }
    }
}

private fun handleBack(state: AppState, viewModel: RuleMindViewModel) {
    when (state.route) {
        "access" -> Unit
        "experience_selection" -> viewModel.goTo("access")
        "home_dashboard", "scenario_hub", "logs_explainability", "admin_console", "sandbox" -> viewModel.goTo("experience_selection")
        else -> viewModel.previousJourneyScreen()
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun StudioTopBar(state: AppState, onBack: () -> Unit, onLogout: () -> Unit) {
    val title = when (state.route) {
        "access" -> "RuleMind Studio"
        "experience_selection" -> "Experience Selection"
        "home_dashboard" -> "Home Dashboard"
        "scenario_hub" -> "Scenario Hub"
        "logs_explainability" -> "Logs & Explainability"
        "admin_console" -> "Admin Console"
        "sandbox" -> "Dynamic Sandbox"
        else -> state.content.journeys.firstOrNull { journey -> journey.screens.any { it.id == state.route } }?.title ?: "RuleMind Studio"
    }
    CenterAlignedTopAppBar(
        title = {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis)
                state.session.connectionStatus?.let {
                    Text(
                        it,
                        modifier = Modifier.testTag("status_connection"),
                        fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        },
        navigationIcon = {
            if (state.route != "access") {
                IconButton(onClick = onBack) {
                    Icon(Icons.Filled.ArrowBack, contentDescription = "Back")
                }
            }
        },
        actions = {
            if (state.session.mode != null) {
                IconButton(onClick = onLogout) {
                    Icon(Icons.Filled.Logout, contentDescription = "Logout")
                }
            }
        },
        colors = TopAppBarDefaults.centerAlignedTopAppBarColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow),
    )
}

@Composable
private fun StudioBottomBar(state: AppState, onNavigate: (String) -> Unit) {
    NavigationBar(containerColor = MaterialTheme.colorScheme.surfaceContainerHigh) {
        val sections = state.content.sections.filter { it.id in setOf("home_dashboard", "scenario_hub", "logs_explainability", "admin_console") }
        sections.forEach { section ->
            NavigationBarItem(
                selected = state.route == section.id,
                onClick = { onNavigate(section.id) },
                modifier = Modifier.testTag("nav_${section.id}"),
                icon = { Icon(iconFor(section.id), contentDescription = section.title) },
                label = { Text(section.title) },
            )
        }
    }
}

@Composable
private fun AccessScreen(viewModel: RuleMindViewModel) {
    val state by viewModel.state.collectAsState()
    val scope = rememberCoroutineScope()
    val context = androidx.compose.ui.platform.LocalContext.current
    Column(
        modifier = Modifier
            .testTag("route_access")
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(20.dp),
    ) {
        HeroHeader(
            eyebrow = "RuleMind Experience Studio",
            title = state.content.appName,
            subtitle = "A single super-app for travel, loan, and SME decision journeys with explainability, workflow orchestration, and mobile authoring.",
        )
        StudioCard {
            Text("Offline Demo Studio", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(8.dp))
            Text("Use the bundled multi-journey experience locally, including scenarios, callbacks, review pauses, and audit views.")
            Spacer(Modifier.height(16.dp))
            Button(
                onClick = { scope.launch { viewModel.startDemo(context) } },
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("action_enter_demo"),
            ) {
                Text("Enter Demo Studio")
            }
        }
        StudioCard {
            Text("Admin Live Mode", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = state.session.baseUrl,
                onValueChange = { viewModel.updateBaseUrl(it) },
                modifier = Modifier.fillMaxWidth().testTag("input_server_url"),
                label = { Text("Server URL") },
                isError = state.loginErrors["url"] != null,
                supportingText = state.loginErrors["url"]?.let { { Text(it, color = MaterialTheme.colorScheme.error) } },
                colors = editorialFieldColors(),
            )
            OutlinedTextField(
                value = state.session.email,
                onValueChange = { viewModel.updateEmail(it) },
                modifier = Modifier.fillMaxWidth().testTag("input_email"),
                label = { Text("Email") },
                isError = state.loginErrors["email"] != null,
                supportingText = state.loginErrors["email"]?.let { { Text(it, color = MaterialTheme.colorScheme.error) } },
                colors = editorialFieldColors(),
            )
            PasswordField(state.session.password, "input_password") { viewModel.updatePassword(it) }
            Spacer(Modifier.height(12.dp))
            Button(
                onClick = { scope.launch { viewModel.loginLive(context) } },
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("action_connect_live"),
            ) {
                if (state.isLoading) {
                    CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                } else {
                    Text("Connect To Live Studio")
                }
            }
        }
        state.error?.let { ErrorText(it) }
    }
}

@Composable
private fun ExperienceSelectionScreen(state: AppState, viewModel: RuleMindViewModel) {
    Column(
        modifier = Modifier
            .testTag("route_experience_selection")
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        HeroHeader("Choose A Journey", "Select an experience lane or head to the home dashboard.", state.content.tagline)
        Button(
            onClick = { viewModel.openHome() },
            modifier = Modifier
                .fillMaxWidth()
                .testTag("action_open_home_dashboard"),
        ) {
            Text("Open Home Dashboard")
        }
        state.content.journeys.forEach { journey ->
            JourneyOverviewCard(journey = journey, onOpen = { viewModel.selectJourney(journey.id) })
        }
    }
}

@Composable
private fun HomeDashboardScreen(state: AppState, viewModel: RuleMindViewModel) {
    val context = androidx.compose.ui.platform.LocalContext.current
    Column(
        modifier = Modifier
            .testTag("route_home_dashboard")
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(18.dp),
    ) {
        HeroHeader(
            eyebrow = state.session.mode?.uppercase() ?: "STUDIO",
            title = "Decision Journeys In Motion",
            subtitle = "Navigate live or demo decisioning across travel, lending, and SME underwriting with explainability and admin controls in one shell.",
        )
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
            Button(
                onClick = { viewModel.syncBundle(context) },
                modifier = Modifier
                    .weight(1f)
                    .testTag("action_sync_bundle"),
            ) {
                Icon(Icons.Filled.Refresh, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Sync")
            }
            TextButton(
                onClick = { viewModel.goTo("scenario_hub") },
                modifier = Modifier
                    .weight(1f)
                    .testTag("action_open_scenarios"),
            ) {
                Icon(Icons.Filled.Science, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Scenarios")
            }
        }
        TextButton(
            onClick = { viewModel.goTo("sandbox") },
            modifier = Modifier
                .fillMaxWidth()
                .testTag("action_open_sandbox"),
        ) {
            Icon(Icons.Filled.Science, contentDescription = null)
            Spacer(Modifier.width(8.dp))
            Text("Open Sandbox")
        }
        state.content.logMetrics.chunked(2).forEach { row ->
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
                row.forEach { metric ->
                    MetricCard(metric.label, metric.value, metric.trend, Modifier.weight(1f))
                }
                if (row.size == 1) {
                    Spacer(Modifier.weight(1f))
                }
            }
        }
        state.content.journeys.forEach { journey ->
            JourneyDashboardCard(
                journey = journey,
                decision = state.decisions[journey.id],
                onResume = { viewModel.selectJourney(journey.id) },
                onRun = { viewModel.selectJourney(journey.id) },
            )
        }
    }
}

@Composable
private fun ScenarioHubScreen(state: AppState, viewModel: RuleMindViewModel) {
    val context = androidx.compose.ui.platform.LocalContext.current
    Column(
        modifier = Modifier
            .testTag("route_scenario_hub")
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        HeroHeader("Deterministic Personas", "Run seeded scenarios across all journeys.", "Each scenario drives the full RuleMind flow, including review pauses, callback queues, and explainability.")
        state.content.journeys.forEach { journey ->
            Text(journey.title, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
            state.content.scenarios.filter { it.journeyId == journey.id }.forEach { scenario ->
                StudioCard(modifier = Modifier.testTag("scenario_card_${scenario.id}")) {
                    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text(scenario.title, fontWeight = FontWeight.SemiBold)
                            Text("${scenario.badge} • expects ${scenario.expectedOutcome.uppercase()} / ${scenario.expectedStatus}", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        AssistChip(onClick = {}, label = { Text(scenario.badge) })
                    }
                    Spacer(Modifier.height(12.dp))
                    Button(
                        onClick = {
                            val processingRoute = journey.screens.firstOrNull { it.type == "processing" }?.id
                                ?: journey.screens.first().id
                            viewModel.goTo(processingRoute, journey.id)
                            viewModel.runScenarioAsync(context, scenario.id)
                        },
                        modifier = Modifier
                            .fillMaxWidth()
                            .testTag("action_run_scenario_${scenario.id}"),
                    ) {
                        Icon(Icons.Filled.PlayArrow, contentDescription = null)
                        Spacer(Modifier.width(8.dp))
                        Text("Run Scenario")
                    }
                }
            }
        }
    }
}

@Composable
private fun LogsScreen(state: AppState, viewModel: RuleMindViewModel) {
    Column(
        modifier = Modifier
            .testTag("route_logs_explainability")
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        HeroHeader("Logs & Explainability", "Audit every execution and callback.", "Execution history, queued operations, score breakdowns, and step traces are surfaced here without reconstructing state in the UI.")
        state.history.forEach { summary ->
            StudioCard(modifier = Modifier.testTag("history_${summary.executionId ?: summary.journeyId}")) {
                Text("${state.content.journeys.firstOrNull { it.id == summary.journeyId }?.title ?: summary.journeyId} • ${summary.outcome.uppercase()}", fontWeight = FontWeight.SemiBold)
                Text("${summary.status} • ${formatNumber(summary.score)} • ${summary.timestamp}", color = MaterialTheme.colorScheme.onSurfaceVariant)
                summary.executionId?.let { Text("Execution ID: $it", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant) }
            }
        }
        Spacer(Modifier.height(8.dp))
        state.logs.reversed().forEach { entry ->
            Text(
                text = "${entry.timestamp}  ${entry.message}",
                color = if (entry.isError) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface,
                fontSize = 13.sp,
            )
        }
    }
}

@Composable
private fun AdminConsoleScreen(state: AppState, viewModel: RuleMindViewModel) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val schema = state.content.adminEntities.firstOrNull { it.id == state.admin.selectedEntityId }
    Column(
        modifier = Modifier
            .testTag("route_admin_console")
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        HeroHeader("Admin Console", "Mobile-safe authoring and controls.", "Use live mode to inspect tenants, edit rules, policies, scorecards, connectors, schedules, settings, and other production-facing assets.")
        if (state.session.availableTenants.isNotEmpty()) {
            Text("Tenant Context", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            state.session.availableTenants.forEach { tenant ->
                FilterChip(
                    selected = tenant.id == state.session.tenantId,
                    onClick = { viewModel.switchTenant(context, tenant.id) },
                    modifier = Modifier.testTag("tenant_${tenant.id}"),
                    label = { Text(tenant.name) },
                )
                Spacer(Modifier.height(8.dp))
            }
        }
        Text("Tools", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
        state.content.adminTools.forEach { tool ->
            StudioCard {
                Text(tool.title, fontWeight = FontWeight.SemiBold)
                Text(tool.description, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        Text("Entities", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
        state.content.adminEntities.forEach { entity ->
            FilterChip(
                selected = entity.id == state.admin.selectedEntityId,
                onClick = {
                    viewModel.selectAdminEntity(entity.id)
                    viewModel.loadAdminRecords(entity.id)
                },
                modifier = Modifier.testTag("admin_entity_${entity.id}"),
                label = { Text(entity.title) },
            )
            Spacer(Modifier.height(8.dp))
        }
        if (state.session.mode != "live") {
            StudioCard {
                Text("Live sign-in required", fontWeight = FontWeight.SemiBold)
                Text("Demo mode keeps the authoring surfaces visible, but create, update, and delete operations are only enabled against a live RuleMind backend.")
            }
        }
        state.admin.status?.let { Text(it, color = MaterialTheme.colorScheme.primary) }
        if (schema != null) {
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
                Button(
                    onClick = { viewModel.startAdminCreate() },
                    enabled = schema.supportsCreate && state.session.mode == "live",
                    modifier = Modifier
                        .weight(1f)
                        .testTag("action_admin_create"),
                ) {
                    Text("Create ${schema.title.removeSuffix("s")}")
                }
                TextButton(
                    onClick = { viewModel.loadAdminRecords(schema.id) },
                    modifier = Modifier
                        .weight(1f)
                        .testTag("action_admin_refresh"),
                ) {
                    Text("Refresh")
                }
            }
            state.admin.records[schema.id].orEmpty().forEach { record ->
                AdminRecordCard(
                    record = record,
                    onEdit = { viewModel.editAdminRecord(record) },
                    onDelete = { viewModel.deleteAdminRecord(record.id) },
                    canDelete = schema.supportsDelete && state.session.mode == "live",
                )
            }
            if (state.admin.isCreating || state.admin.selectedRecordId != null) {
                AdminEditor(schema = schema, state = state, viewModel = viewModel)
            }
        }
    }
}

@Composable
private fun JourneyScreen(state: AppState, viewModel: RuleMindViewModel) {
    val journey = state.content.journeys.firstOrNull { item -> item.screens.any { screen -> screen.id == state.route } } ?: return
    val screen = journey.screens.firstOrNull { it.id == state.route } ?: return
    val decision = state.decisions[journey.id]
    val context = androidx.compose.ui.platform.LocalContext.current
    val scope = rememberCoroutineScope()
    val draft = state.drafts[journey.id].orEmpty()
    Column(
        modifier = Modifier
            .testTag("route_${screen.id}")
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        JourneyHeader(journey = journey, screen = screen)
        when (screen.type) {
            "landing" -> {
                HeroHeader(journey.category, screen.title, screen.subtitle)
                screen.bullets.forEach { bullet -> BulletLine(bullet) }
                Button(
                    onClick = { viewModel.nextJourneyScreen() },
                    modifier = Modifier
                        .fillMaxWidth()
                        .testTag("action_begin_${journey.id}"),
                ) {
                    Text("Begin ${journey.title}")
                }
            }
            "form" -> {
                screen.fields.forEach { field ->
                    val fieldError = state.fieldErrors[journey.id]?.get(field.id)
                    JourneyField(field = field, value = draft[field.id], onChange = { viewModel.updateDraftField(journey.id, field, it) }, error = fieldError)
                }
                NavigationRow(
                    onBack = { viewModel.previousJourneyScreen() },
                    onNext = { viewModel.nextJourneyScreen() },
                    nextLabel = "Continue",
                )
            }
            "processing" -> {
                ProcessingCard(decision = decision, title = screen.title, subtitle = screen.subtitle)
                Button(
                    onClick = { scope.launch { viewModel.evaluateJourney(context, journey.id) } },
                    modifier = Modifier
                        .fillMaxWidth()
                        .testTag("action_run_decision"),
                ) {
                    Text(if (decision == null) "Run RuleMind Decision" else "Re-run Decision")
                }
                TextButton(
                    onClick = { viewModel.nextJourneyScreen() },
                    modifier = Modifier
                        .fillMaxWidth()
                        .testTag("action_continue_after_processing"),
                ) {
                    Text("Continue To Next Step")
                }
            }
            "outcome" -> {
                OutcomeScreenBody(journey = journey, decision = decision, onRun = { scope.launch { viewModel.evaluateJourney(context, journey.id) } }, onFlush = {
                    viewModel.flushPendingOperations(context, journey.id)
                }, onReview = { approve ->
                    viewModel.resumeReview(context, journey.id, approve)
                }, onAudit = { viewModel.nextJourneyScreen() })
            }
            "audit" -> {
                AuditScreenBody(decision = decision, onFlush = { viewModel.flushPendingOperations(context, journey.id) })
            }
        }
    }
}

@Composable
private fun SandboxScreen(state: AppState, viewModel: RuleMindViewModel) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val sandbox = state.sandbox
    val bundle = state.bundle
    val policies = bundle?.policies.orEmpty()
    Column(
        modifier = Modifier
            .testTag("route_sandbox")
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        HeroHeader("Dynamic Sandbox", "Evaluate any policy with arbitrary input data.", "Select a policy, add key-value fields, choose types, and run the rule engine offline.")
        StudioCard {
            Text("Policy", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(8.dp))
            var expanded by remember { mutableStateOf(false) }
            Box {
                OutlinedTextField(
                    value = sandbox.selectedPolicyId ?: "Select a policy...",
                    onValueChange = {},
                    modifier = Modifier.fillMaxWidth().testTag("sandbox_policy_select"),
                    readOnly = true,
                    colors = editorialFieldColors(),
                    trailingIcon = {
                        TextButton(onClick = { expanded = true }) { Text("Choose") }
                    },
                )
                androidx.compose.material3.DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
                    policies.forEach { policy ->
                        val policyId = (policy as? Map<*, *>)?.get("id")?.toString()
                            ?: policy.toString()
                        androidx.compose.material3.DropdownMenuItem(
                            text = { Text(policyId) },
                            onClick = {
                                viewModel.selectSandboxPolicy(policyId)
                                expanded = false
                            },
                        )
                    }
                }
            }
        }
        StudioCard {
            Text("Input Fields", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(8.dp))
            sandbox.fields.forEachIndexed { index, field ->
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    OutlinedTextField(
                        value = field.key,
                        onValueChange = { viewModel.updateSandboxField(index, key = it) },
                        modifier = Modifier.weight(1f).testTag("sandbox_key_$index"),
                        label = { Text("Key") },
                        singleLine = true,
                        colors = editorialFieldColors(),
                    )
                    OutlinedTextField(
                        value = field.value,
                        onValueChange = { viewModel.updateSandboxField(index, value = it) },
                        modifier = Modifier.weight(1f).testTag("sandbox_value_$index"),
                        label = { Text("Value") },
                        singleLine = true,
                        colors = editorialFieldColors(),
                    )
                    var typeExpanded by remember { mutableStateOf(false) }
                    Box {
                        AssistChip(
                            onClick = { typeExpanded = true },
                            label = { Text(field.type) },
                            modifier = Modifier.testTag("sandbox_type_$index"),
                        )
                        androidx.compose.material3.DropdownMenu(expanded = typeExpanded, onDismissRequest = { typeExpanded = false }) {
                            listOf("string", "number", "boolean").forEach { type ->
                                androidx.compose.material3.DropdownMenuItem(
                                    text = { Text(type) },
                                    onClick = {
                                        viewModel.updateSandboxField(index, type = type)
                                        typeExpanded = false
                                    },
                                )
                            }
                        }
                    }
                    IconButton(
                        onClick = { viewModel.removeSandboxField(index) },
                        modifier = Modifier.testTag("sandbox_remove_$index"),
                    ) {
                        Icon(Icons.Filled.Clear, contentDescription = "Remove")
                    }
                }
                Spacer(Modifier.height(8.dp))
            }
            TextButton(
                onClick = { viewModel.addSandboxField() },
                modifier = Modifier.testTag("sandbox_add_field"),
            ) {
                Icon(Icons.Filled.Add, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Add Field")
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
            Button(
                onClick = { viewModel.evaluateSandbox(context) },
                modifier = Modifier.weight(1f).testTag("sandbox_evaluate"),
            ) {
                Icon(Icons.Filled.PlayArrow, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Run Evaluation")
            }
            TextButton(
                onClick = { viewModel.clearSandbox() },
                modifier = Modifier.weight(1f).testTag("sandbox_clear"),
            ) {
                Text("Clear")
            }
        }
        sandbox.error?.let { ErrorText(it) }
        sandbox.result?.let { decision ->
            OutcomeBanner(outcome = decision.outcome, latencyMs = decision.latencyMs)
            decision.score?.let { score ->
                StudioCard {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                        ScoreGauge(score = score, maxScore = 900.0, label = "Sandbox Score")
                        Column(modifier = Modifier.weight(1f)) {
                            Text("Score Summary", fontWeight = FontWeight.SemiBold)
                            Text("Trace steps: ${decision.trace.size}", fontSize = 12.sp)
                        }
                    }
                }
            }
            DecisionSummaryCard(decision = decision)
        }
    }
}

@Composable
private fun JourneyHeader(journey: StudioJourney, screen: StudioScreen) {
    StudioCard {
        Text(journey.title, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
        Text(screen.title, style = MaterialTheme.typography.headlineSmall)
        Text(screen.subtitle, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun ProcessingCard(decision: Decision?, title: String, subtitle: String) {
    StudioCard {
        Text(title, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
        Text(subtitle, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(12.dp))
        if (decision == null) {
            Text("No execution has been run yet for this journey.")
        } else {
            Text("Outcome: ${decision.outcome.uppercase()} • Status: ${decision.status}")
            Text("Callbacks queued: ${decision.pendingOperations.count { it["status"] != "delivered" }}")
            Text("Trace steps: ${decision.trace.size}")
        }
    }
}

@Composable
private fun OutcomeScreenBody(
    journey: StudioJourney,
    decision: Decision?,
    onRun: () -> Unit,
    onFlush: () -> Unit,
    onReview: (Boolean) -> Unit,
    onAudit: () -> Unit,
) {
    if (decision == null) {
        StudioCard(modifier = Modifier.testTag("decision_pending")) {
            Text("No decision yet", fontWeight = FontWeight.SemiBold)
            Text("Complete the forms and run the decision to populate outcome, callbacks, and explainability.")
            Spacer(Modifier.height(12.dp))
            Button(
                onClick = onRun,
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("action_run_decision"),
            ) {
                Text("Run Decision")
            }
        }
        return
    }
    OutcomeBanner(outcome = decision.outcome, latencyMs = decision.latencyMs)
    decision.score?.let { score ->
        ScoreSection(journey = journey, decision = decision, score = score)
    }
    DecisionSummaryCard(decision = decision)
    if (decision.pendingOperations.any { it["status"] != "delivered" }) {
        Button(
            onClick = onFlush,
            modifier = Modifier
                .fillMaxWidth()
                .testTag("action_flush_pending_callbacks"),
        ) {
            Icon(Icons.Filled.Refresh, contentDescription = null)
            Spacer(Modifier.width(8.dp))
            Text("Flush Pending Callbacks")
        }
    }
    if (decision.status == "paused") {
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
            Button(
                onClick = { onReview(true) },
                modifier = Modifier
                    .weight(1f)
                    .testTag("action_review_approve"),
            ) {
                Icon(Icons.Filled.Check, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Approve Review")
            }
            TextButton(
                onClick = { onReview(false) },
                modifier = Modifier
                    .weight(1f)
                    .testTag("action_review_reject"),
            ) {
                Text("Reject Review")
            }
        }
    }
    TextButton(
        onClick = onAudit,
        modifier = Modifier
            .fillMaxWidth()
            .testTag("action_open_audit"),
    ) {
        Text("Open Explainability & Audit")
    }
}

@Composable
private fun AuditScreenBody(decision: Decision?, onFlush: () -> Unit) {
    if (decision == null) {
        StudioCard(modifier = Modifier.testTag("trace_empty")) { Text("Run a decision to unlock explainability.") }
        return
    }
    DecisionSummaryCard(decision)
    decision.explainability["scorecards"]?.let { scorecards ->
        StudioCard(modifier = Modifier.testTag("scorecard_breakdown")) {
            Text("Scorecard Breakdown", fontWeight = FontWeight.SemiBold)
            Text(scorecards.toString(), fontSize = 12.sp)
        }
    }
    decision.trace.forEachIndexed { index, item ->
        val step = item["step"] as? Map<*, *>
        val result = item["result"] as? Map<*, *>
        StudioCard(modifier = Modifier.testTag("trace_item_$index")) {
            Text("${index + 1}. ${step?.get("label") ?: step?.get("type") ?: "Step"}", fontWeight = FontWeight.SemiBold)
            Text("Type: ${step?.get("type") ?: "unknown"}")
            if (item["skipped"] == true) {
                Text(item["reason"]?.toString() ?: "Skipped", color = MaterialTheme.colorScheme.onSurfaceVariant)
            } else {
                Text(result?.toString() ?: "Executed", fontSize = 12.sp)
            }
        }
    }
    if (decision.pendingOperations.any { it["status"] != "delivered" }) {
        Button(
            onClick = onFlush,
            modifier = Modifier
                .fillMaxWidth()
                .testTag("action_deliver_pending_operations"),
        ) {
            Text("Deliver Pending Operations")
        }
    }
}

@Composable
private fun DecisionSummaryCard(decision: Decision) {
    StudioCard(modifier = Modifier.testTag("decision_summary")) {
        Text("Decision Summary", fontWeight = FontWeight.SemiBold)
        Text("Outcome: ${decision.outcome.uppercase()} • Status: ${decision.status}")
        Text("Source: ${decision.source ?: "local"}")
        Text("Execution ID: ${decision.executionId ?: "Not assigned"}", modifier = Modifier.testTag("execution_id"), fontSize = 12.sp)
        Text("Request ID: ${decision.requestId ?: "—"}", modifier = Modifier.testTag("request_id"), fontSize = 12.sp)
        if (decision.variables.isNotEmpty()) {
            Spacer(Modifier.height(10.dp))
            Text("Computed Variables", fontWeight = FontWeight.SemiBold)
            decision.variables.entries.take(8).forEach { entry ->
                Text("${entry.key}: ${formatAny(entry.value)}", fontSize = 12.sp)
            }
        }
        if (decision.ruleResults.isNotEmpty()) {
            Spacer(Modifier.height(10.dp))
            Text("Rule Results", fontWeight = FontWeight.SemiBold)
            decision.ruleResults.forEach { result ->
                Text("${result["ruleId"] ?: result["rule_id"]}: ${result["outcome"]}", fontSize = 12.sp)
            }
        }
    }
}

@Composable
private fun ScoreSection(journey: StudioJourney, decision: Decision, score: Double) {
    StudioCard {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(16.dp)) {
            ScoreGauge(score = score, maxScore = 900.0, label = if (journey.id == "instant_personal_loan") "Risk Score" else "Journey Score")
            Column(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (journey.id == "instant_personal_loan") {
                    Text("Bureau Score: ${formatAny(decision.variables["loan_bureau_score"])}", fontWeight = FontWeight.SemiBold)
                    Text("Risk Score is the scorecard result, not the applicant's bureau input.", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp)
                } else {
                    Text("Score Summary", fontWeight = FontWeight.SemiBold)
                }
                Text("Callback ops: ${decision.pendingOperations.count { it["status"] != "delivered" }}", fontSize = 12.sp)
                Text("Trace steps: ${decision.trace.size}", fontSize = 12.sp)
            }
        }
    }
}

@Composable
private fun JourneyField(field: StudioField, value: Any?, onChange: (String) -> Unit, error: String? = null) {
    when {
        field.kind == "choice" && !field.multi -> {
            var expanded by remember { mutableStateOf(false) }
            Box {
                OutlinedTextField(
                    value = value?.toString().orEmpty(),
                    onValueChange = {},
                    modifier = Modifier
                        .fillMaxWidth()
                        .testTag("field_${field.id}"),
                    readOnly = true,
                    label = { Text(field.label) },
                    isError = error != null,
                    colors = editorialFieldColors(),
                    trailingIcon = {
                        TextButton(onClick = { expanded = true }) { Text("Choose") }
                    },
                )
                androidx.compose.material3.DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
                    field.options.forEach { option ->
                        androidx.compose.material3.DropdownMenuItem(
                            text = { Text(option) },
                            onClick = {
                                onChange(option)
                                expanded = false
                            },
                        )
                    }
                }
            }
        }
        else -> {
            OutlinedTextField(
                value = when (value) {
                    is List<*> -> value.joinToString(", ")
                    else -> value?.toString().orEmpty()
                },
                onValueChange = onChange,
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("field_${field.id}"),
                label = { Text(field.label) },
                isError = error != null,
                minLines = if (field.kind in setOf("json", "code")) 5 else 1,
                keyboardOptions = KeyboardOptions(keyboardType = if (field.kind == "number") KeyboardType.Decimal else KeyboardType.Text),
                colors = editorialFieldColors(),
                supportingText = {
                    if (error != null) {
                        Text(error, color = MaterialTheme.colorScheme.error)
                    } else {
                        val hint = buildString {
                            if (field.required) append("Required")
                            if (field.hint.isNotBlank()) {
                                if (isNotBlank()) append(" • ")
                                append(field.hint)
                            }
                        }
                        if (hint.isNotBlank()) Text(hint)
                    }
                },
            )
        }
    }
}

@Composable
private fun NavigationRow(onBack: () -> Unit, onNext: () -> Unit, nextLabel: String) {
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
        TextButton(onClick = onBack, modifier = Modifier.weight(1f).testTag("action_back")) { Text("Back") }
        Button(onClick = onNext, modifier = Modifier.weight(1f).testTag("action_continue")) { Text(nextLabel) }
    }
}

@Composable
private fun JourneyOverviewCard(journey: StudioJourney, onOpen: () -> Unit) {
    StudioCard(modifier = Modifier.testTag("journey_card_${journey.id}")) {
        Text(journey.title, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
        Text(journey.category, color = MaterialTheme.colorScheme.primary)
        Text("${journey.stepCount} curated screens powered by the same rule engine, callbacks, and explainability contract.")
        Spacer(Modifier.height(12.dp))
        Button(
            onClick = onOpen,
            modifier = Modifier
                .fillMaxWidth()
                .testTag("action_open_journey_${journey.id}"),
        ) {
            Text("Open Journey")
        }
    }
}

@Composable
private fun JourneyDashboardCard(journey: StudioJourney, decision: Decision?, onResume: () -> Unit, onRun: () -> Unit) {
    StudioCard(modifier = Modifier.testTag("dashboard_card_${journey.id}")) {
        Text(journey.title, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
        Text(journey.category, color = MaterialTheme.colorScheme.onSurfaceVariant)
        decision?.let {
            Spacer(Modifier.height(8.dp))
            Text("Latest: ${it.outcome.uppercase()} • ${it.status}", fontWeight = FontWeight.SemiBold)
            Text("Score: ${formatNumber(it.score)}", fontSize = 12.sp)
        }
        Spacer(Modifier.height(12.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
            Button(onClick = onRun, modifier = Modifier.weight(1f).testTag("action_dashboard_open_${journey.id}")) {
                Text(if (decision == null) "Start" else "Open")
            }
            TextButton(onClick = onResume, modifier = Modifier.weight(1f).testTag("action_dashboard_resume_${journey.id}")) {
                Text("Resume")
            }
        }
    }
}

@Composable
private fun AdminRecordCard(record: AdminRecord, onEdit: () -> Unit, onDelete: () -> Unit, canDelete: Boolean) {
    StudioCard(modifier = Modifier.testTag("admin_record_${record.id}")) {
        Text(record.title, fontWeight = FontWeight.SemiBold)
        Text(record.id, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(12.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
            Button(onClick = onEdit, modifier = Modifier.weight(1f).testTag("action_admin_edit_${record.id}")) { Text("Edit") }
            TextButton(onClick = onDelete, enabled = canDelete, modifier = Modifier.weight(1f).testTag("action_admin_delete_${record.id}")) { Text("Delete") }
        }
    }
}

@Composable
private fun AdminEditor(schema: StudioEntitySchema, state: AppState, viewModel: RuleMindViewModel) {
    StudioCard(modifier = Modifier.testTag("admin_editor")) {
        Text(
            if (state.admin.isCreating) "Create ${schema.title.removeSuffix("s")}" else "Edit ${schema.title.removeSuffix("s")}",
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.SemiBold,
        )
        Spacer(Modifier.height(12.dp))
        schema.fields.forEach { field ->
            JourneyField(field = field, value = state.admin.editorRawValues[field.id], onChange = { viewModel.updateAdminField(field, it) })
            Spacer(Modifier.height(8.dp))
        }
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
            TextButton(onClick = { viewModel.cancelAdminEdit() }, modifier = Modifier.weight(1f).testTag("action_admin_cancel")) {
                Text("Cancel")
            }
            Button(onClick = { viewModel.saveAdminRecord() }, modifier = Modifier.weight(1f).testTag("action_admin_save")) {
                Text("Save")
            }
        }
    }
}

@Composable
private fun HeroHeader(eyebrow: String, title: String, subtitle: String) {
    StudioCard {
        Text(eyebrow.uppercase(), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.primary)
        Spacer(Modifier.height(10.dp))
        Text(title, style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(10.dp))
        Text(subtitle, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun BulletLine(text: String) {
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(modifier = Modifier.size(8.dp).background(MaterialTheme.colorScheme.primary, CircleShape))
        Text(text)
    }
}

@Composable
private fun LabeledField(label: String, value: String, tag: String, onChange: (String) -> Unit) {
    OutlinedTextField(
        value = value,
        onValueChange = onChange,
        modifier = Modifier.fillMaxWidth().testTag(tag),
        label = { Text(label) },
        colors = editorialFieldColors(),
    )
}

@Composable
private fun PasswordField(value: String, tag: String, onChange: (String) -> Unit) {
    OutlinedTextField(
        value = value,
        onValueChange = onChange,
        modifier = Modifier.fillMaxWidth().testTag(tag),
        label = { Text("Password") },
        colors = editorialFieldColors(),
    )
}

@Composable
private fun MetricCard(label: String, value: String, trend: String, modifier: Modifier = Modifier) {
    StudioCard(modifier = modifier) {
        Text(label, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(6.dp))
        Text(value, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(4.dp))
        Text(trend, fontSize = 12.sp, color = MaterialTheme.colorScheme.primary)
    }
}

@Composable
private fun ErrorText(text: String) {
    Text(text, color = MaterialTheme.colorScheme.error)
}

@Composable
private fun StudioCard(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Card(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(28.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLowest),
    ) {
        Column(modifier = Modifier.padding(22.dp), content = content)
    }
}

@Composable
private fun editorialFieldColors() = OutlinedTextFieldDefaults.colors(
    unfocusedContainerColor = MaterialTheme.colorScheme.surfaceContainerHigh,
    focusedContainerColor = MaterialTheme.colorScheme.surfaceContainerLowest,
    cursorColor = MaterialTheme.colorScheme.primary,
    focusedBorderColor = MaterialTheme.colorScheme.primary.copy(alpha = 0.3f),
    unfocusedBorderColor = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.15f),
)

@Composable
private fun iconFor(route: String) = when (route) {
    "home_dashboard" -> Icons.Filled.Home
    "scenario_hub" -> Icons.Filled.Science
    "logs_explainability" -> Icons.Filled.Terminal
    "admin_console" -> Icons.Filled.Settings
    "travel_guard" -> Icons.Filled.FlightTakeoff
    "instant_personal_loan" -> Icons.Filled.AccountBalance
    else -> Icons.Filled.Business
}

private fun formatAny(value: Any?): String = when (value) {
    null -> "—"
    is Number -> formatNumber(value.toDouble())
    is List<*> -> value.joinToString(", ") { formatAny(it) }
    is Map<*, *> -> value.toString()
    else -> value.toString()
}

private fun formatNumber(value: Double?): String {
    if (value == null) return "—"
    val rounded = kotlin.math.round(value * 1000.0) / 1000.0
    return if (rounded % 1.0 == 0.0) rounded.toInt().toString() else rounded.toString()
}
