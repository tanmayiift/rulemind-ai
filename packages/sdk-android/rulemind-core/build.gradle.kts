plugins {
    kotlin("jvm")
    `maven-publish`
}

val activeJvm = JavaVersion.current().majorVersion.toIntOrNull() ?: 17
val toolchainJvm = if (activeJvm in 17..21) activeJvm else 17

kotlin {
    jvmToolchain(toolchainJvm)
}

dependencies {
    testImplementation(kotlin("test"))
    testImplementation("org.json:json:20240303")
    // runBlocking for the suspend-based DecisionSyncer/outbox tests.
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.11.0")
}

// The conformance suites read their corpora from packages/shared at RUNTIME, by relative path,
// so Gradle sees no dependency on them: regenerating a corpus left `:rulemind-core:test`
// UP-TO-DATE and the suite silently did not re-run. Declaring the files as inputs is what makes
// a corpus change actually re-check this engine locally. (CI runs on a fresh checkout with no
// build cache, so it was always correct there -- which is precisely why this went unnoticed.)
tasks.withType<Test>().configureEach {
    inputs.files(fileTree(rootProject.file("../shared")) { include("*.spec.json") })
        .withPropertyName("sharedConformanceCorpora")
        .withPathSensitivity(PathSensitivity.RELATIVE)
}

publishing {
    publications {
        create<MavenPublication>("release") {
            from(components["java"])
            groupId = "com.rulemind"
            artifactId = "rulemind-core"
            version = project.version.toString()
        }
    }
}
