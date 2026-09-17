plugins {
    kotlin("jvm") version "2.2.20" apply false
    id("com.android.library") version "9.4.0" apply false
    id("com.android.application") version "9.4.0" apply false
    kotlin("android") version "2.2.20" apply false
    // Required from Kotlin 2.0 onwards for any module with Compose enabled: the Compose compiler
    // stopped shipping as a standalone artifact and became a Kotlin compiler plugin, versioned
    // WITH Kotlin rather than independently. Without it the sample app fails configuration with
    // "the Compose Compiler Gradle plugin is required when compose is enabled".
    id("org.jetbrains.kotlin.plugin.compose") version "2.2.20" apply false
    `maven-publish`
}

allprojects {
    group = "com.rulemind"
    version = "4.1.0-beta.1"
}
