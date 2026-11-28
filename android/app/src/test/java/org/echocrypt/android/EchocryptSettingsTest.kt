package org.echocrypt.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class EchocryptSettingsTest {
    @Test fun defaultsAreValid() {
        assertEquals(EchocryptSettings(), EchocryptSettings().validate())
    }
    @Test fun unsupportedValuesAreRejected() {
        assertThrows(IllegalArgumentException::class.java) {
            EchocryptSettings(preferredOutputSampleRate = 11_025).validate()
        }
        assertThrows(IllegalArgumentException::class.java) {
            EchocryptSettings(directPaddingBlock = 48).validate()
        }
    }
}
