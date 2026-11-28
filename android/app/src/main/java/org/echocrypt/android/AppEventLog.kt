package org.echocrypt.android

import android.content.Context
import android.os.Environment
import java.io.File
import java.io.FileOutputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Structured bounded log. Message bodies, private keys and file contents are never recorded. */
object AppEventLog {
    private const val DIRECTORY_NAME = "ECHOCRYPT/logs"
    private const val MAX_FILE_BYTES = 2L * 1024L * 1024L
    private const val MAX_LOG_FILES = 20
    @Volatile private var applicationContext: Context? = null
    @Volatile private var actualDirectory: File? = null

    fun initialize(context: Context) {
        applicationContext = context.applicationContext
        record("APP_START", "package=" + context.packageName + " sdk=" + android.os.Build.VERSION.SDK_INT)
    }

    @Synchronized
    fun record(event: String, details: String = "") {
        val context = applicationContext ?: return
        val safeEvent = event.uppercase(Locale.US).replace(Regex("[^A-Z0-9_.-]"), "_").take(48)
        val safeDetails = details.replace('\r', ' ').replace('\n', ' ').take(1200)
        val timestamp = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", Locale.US).format(Date())
        val line = timestamp + " [" + safeEvent + "] " + safeDetails + "\n"
        try {
            val directory = actualDirectory ?: preferredDirectory(context).also {
                if (!it.exists() && !it.mkdirs() && !it.isDirectory) {
                    throw IllegalStateException("Could not create event-log directory: " + it)
                }
                actualDirectory = it
            }
            FileOutputStream(activeFile(directory), true).bufferedWriter(Charsets.UTF_8).use { it.write(line) }
            prune(directory)
        } catch (_: Exception) {
            // Use app-private internal storage when external app-specific storage is unavailable.
            runCatching {
                val fallback = File(context.filesDir, DIRECTORY_NAME)
                if (!fallback.exists()) fallback.mkdirs()
                actualDirectory = fallback
                FileOutputStream(activeFile(fallback), true).bufferedWriter(Charsets.UTF_8).use { it.write(line) }
            }
        }
    }

    fun directoryPath(context: Context): String =
        (actualDirectory ?: preferredDirectory(context.applicationContext)).absolutePath

    fun latestLogFile(context: Context): File? =
        (actualDirectory ?: preferredDirectory(context.applicationContext))
            .listFiles { file -> file.isFile && file.name.endsWith(".log") }
            ?.maxByOrNull { it.lastModified() }

    private fun preferredDirectory(context: Context): File {
        val base = context.getExternalFilesDir(Environment.DIRECTORY_DOCUMENTS)
            ?: File(context.filesDir, "Documents")
        return File(base, DIRECTORY_NAME)
    }

    private fun activeFile(directory: File): File {
        val date = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())
        var suffix = 0
        while (true) {
            val name = if (suffix == 0) "echocrypt-" + date + ".log"
                else "echocrypt-" + date + "-" + suffix + ".log"
            val candidate = File(directory, name)
            if (!candidate.exists() || candidate.length() < MAX_FILE_BYTES) return candidate
            suffix++
        }
    }

    private fun prune(directory: File) {
        val logs = directory.listFiles { file -> file.isFile && file.name.endsWith(".log") }
            ?.sortedByDescending { it.lastModified() } ?: return
        logs.drop(MAX_LOG_FILES).forEach { runCatching { it.delete() } }
    }
}
