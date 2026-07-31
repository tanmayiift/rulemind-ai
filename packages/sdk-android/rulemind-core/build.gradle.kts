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
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.7.3")
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
