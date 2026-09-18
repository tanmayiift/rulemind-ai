plugins {
    id("com.android.library")
    // No kotlin("android"): AGP 9 has Kotlin support BUILT IN and registers its own
    // `kotlin` extension, so declaring the plugin too fails with "Cannot add extension with name
    // 'kotlin'". AGP's own error names this file and this fix.
    `maven-publish`
}

android {
    namespace = "com.rulemind.android"
    compileSdk = 34

    defaultConfig {
        minSdk = 29
        consumerProguardFiles("consumer-rules.pro")
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "consumer-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    // UNIT TESTS FOR THIS MODULE, which had NO test source set at all until 2026-09-18 -- 1,644
    // lines of crypto, networking and persistence with nothing exercising them, while the pure-Kotlin
    // `rulemind-core` next door had 46 tests.
    //
    // Robolectric rather than instrumentation: these run on the JVM in the ordinary `test` task, so
    // they are part of the standing gate instead of waiting for an emulator (issue #133 is exactly
    // that wait). What Robolectric CANNOT give is a real AndroidKeyStore -- see SecurePrefsTest for
    // which half of the crypto that leaves covered and which half still needs a device.
    testOptions {
        unitTests {
            isIncludeAndroidResources = true
            isReturnDefaultValues = true
        }
    }

    publishing {
        singleVariant("release") {
            withSourcesJar()
        }
    }
}

// `android { kotlinOptions { } }` was provided by the kotlin-android plugin, which AGP 9 makes
// redundant and refuses to load alongside. The replacement is the top-level Kotlin DSL.
kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    api(project(":rulemind-core"))
    implementation("androidx.security:security-crypto:1.1.0")
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // Off-main-thread DB I/O for the durable decision outbox (Dispatchers.IO / withContext).
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.11.0")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.robolectric:robolectric:4.14.1")
    testImplementation("androidx.test:core:1.6.1")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.11.0")
}

publishing {
    publications {
        create<MavenPublication>("release") {
            groupId = "com.rulemind"
            artifactId = "rulemind-android"
            version = project.version.toString()
            afterEvaluate {
                from(components["release"])
            }
            pom {
                name.set("RuleMind Android SDK")
                description.set("RuleMind mobile decisioning SDK for Android.")
                url.set("https://github.com/rulemind-ai/rulemind")
                licenses {
                    license {
                        name.set("Apache-2.0")
                        url.set("https://www.apache.org/licenses/LICENSE-2.0")
                    }
                }
                developers {
                    developer {
                        id.set("rulemind")
                        name.set("RuleMind")
                    }
                }
                scm {
                    url.set("https://github.com/rulemind-ai/rulemind")
                }
            }
        }
    }
}
