package org.echocrypt.android

import org.echocrypt.android.protocol.DirectSessionLifecycle
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class DirectSessionLifecycleTest {
    @Test
    fun initiatorRequiresVerifiedReplyAndFinish() {
        val session = DirectSessionLifecycle(DirectSessionLifecycle.Role.INITIATOR)
        assertEquals(DirectSessionLifecycle.State.READY, session.state)
        session.transition(DirectSessionLifecycle.Event.LOCAL_HELLO_SENT)
        assertEquals(DirectSessionLifecycle.State.WAIT_REPLY, session.state)
        session.transition(DirectSessionLifecycle.Event.PEER_HELLO_VERIFIED)
        assertEquals(DirectSessionLifecycle.State.WAIT_RESP_FINISH, session.state)
        assertThrows(IllegalStateException::class.java) { session.requireEstablished() }
        session.transition(DirectSessionLifecycle.Event.PEER_FINISH_VERIFIED)
        session.requireEstablished()
    }

    @Test
    fun responderRequiresHelloAndInitiatorFinish() {
        val session = DirectSessionLifecycle(DirectSessionLifecycle.Role.RESPONDER)
        assertEquals(DirectSessionLifecycle.State.WAIT_HELLO, session.state)
        session.transition(DirectSessionLifecycle.Event.PEER_HELLO_VERIFIED)
        assertEquals(DirectSessionLifecycle.State.WAIT_INIT_FINISH, session.state)
        session.transition(DirectSessionLifecycle.Event.PEER_FINISH_VERIFIED)
        session.requireEstablished()
    }

    @Test
    fun rejectsOutOfOrderEventsAndClosesPermanently() {
        val session = DirectSessionLifecycle(DirectSessionLifecycle.Role.INITIATOR)
        assertThrows(IllegalStateException::class.java) {
            session.transition(DirectSessionLifecycle.Event.PEER_FINISH_VERIFIED)
        }
        session.transition(DirectSessionLifecycle.Event.CLOSE)
        assertThrows(IllegalStateException::class.java) {
            session.transition(DirectSessionLifecycle.Event.LOCAL_HELLO_SENT)
        }
    }
}
