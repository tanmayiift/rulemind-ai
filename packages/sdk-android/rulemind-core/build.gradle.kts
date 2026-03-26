plugins {
    kotlin("jvm")
    `maven-publish`
}

kotlin {
    jvmToolchain(17)
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
