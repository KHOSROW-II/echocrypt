package org.echocrypt.android

import org.echocrypt.android.protocol.ReplayWindow
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ReplayWindowTest {
    @Test
    fun acceptsOutOfOrderPacketsWithinWindowOnce() {
        val window = ReplayWindow(8)
        assertTrue(window.acceptAuthenticated(10))
        assertTrue(window.acceptAuthenticated(12))
        assertTrue(window.acceptAuthenticated(11))
        assertFalse(window.acceptAuthenticated(11))
        assertFalse(window.acceptAuthenticated(10))
    }

    @Test
    fun rejectsPacketsOutsideWindow() {
        val window = ReplayWindow(8)
        assertTrue(window.acceptAuthenticated(20))
        assertTrue(window.acceptAuthenticated(28))
        assertFalse(window.acceptAuthenticated(20))
    }

    @Test
    fun largeForwardJumpClearsOldHistory() {
        val window = ReplayWindow(8)
        assertTrue(window.acceptAuthenticated(1))
        assertTrue(window.acceptAuthenticated(100))
        assertFalse(window.acceptAuthenticated(1))
        assertTrue(window.acceptAuthenticated(99))
    }

    @Test
    fun resetStartsANewSequenceSpace() {
        val window = ReplayWindow()
        assertTrue(window.acceptAuthenticated(7))
        window.reset()
        assertTrue(window.acceptAuthenticated(7))
        assertFalse(window.acceptAuthenticated(-1))
    }
}
