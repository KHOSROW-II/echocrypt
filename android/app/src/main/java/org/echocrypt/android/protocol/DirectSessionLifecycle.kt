package org.echocrypt.android.protocol

/**
 * Explicit Direct-session lifecycle shared by the future authenticated handshake
 * and transport layers. This class does not perform cryptography or send packets.
 */
class DirectSessionLifecycle(val role: Role) {
    enum class Role { INITIATOR, RESPONDER }
    enum class State {
        READY,
        WAIT_HELLO,
        WAIT_REPLY,
        WAIT_INIT_FINISH,
        WAIT_RESP_FINISH,
        ESTABLISHED,
        CLOSED
    }

    enum class Event {
        LOCAL_HELLO_SENT,
        PEER_HELLO_VERIFIED,
        PEER_FINISH_VERIFIED,
        CLOSE
    }

    var state: State = if (role == Role.INITIATOR) State.READY else State.WAIT_HELLO
        private set

    @Synchronized
    fun transition(event: Event): State {
        if (event == Event.CLOSE) {
            state = State.CLOSED
            return state
        }
        state = when (role) {
            Role.INITIATOR -> when (state to event) {
                State.READY to Event.LOCAL_HELLO_SENT -> State.WAIT_REPLY
                State.WAIT_REPLY to Event.PEER_HELLO_VERIFIED -> State.WAIT_RESP_FINISH
                State.WAIT_RESP_FINISH to Event.PEER_FINISH_VERIFIED -> State.ESTABLISHED
                else -> invalid(event)
            }
            Role.RESPONDER -> when (state to event) {
                State.WAIT_HELLO to Event.PEER_HELLO_VERIFIED -> State.WAIT_INIT_FINISH
                State.WAIT_INIT_FINISH to Event.PEER_FINISH_VERIFIED -> State.ESTABLISHED
                else -> invalid(event)
            }
        }
        return state
    }

    @Synchronized
    fun requireEstablished() {
        check(state == State.ESTABLISHED) {
            "Direct session is not established (state=$state)"
        }
    }

    private fun invalid(event: Event): Nothing {
        throw IllegalStateException("Invalid Direct handshake event $event in state $state for role $role")
    }
}
