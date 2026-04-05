plugins {
    kotlin("jvm")
    `maven-publish`
}

val activeJvm = JavaVersion.current().majorVersion.toIntOrNull() ?: 17
val toolchainJvm = activeJvm.coerceIn(17, 21)

kotlin {
    jvmToolchain(toolchainJvm)
}

dependencies {
    testImplementation(kotlin("test"))
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
