from dotenv import load_dotenv
import os
from typing import Any, Optional
from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, Agent, room_io, function_tool, RunContext
from livekit.plugins import (
    openai,
    noise_cancellation,
)
import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor



load_dotenv(".env")
DATABASE_URL = os.getenv("DATABASE_URL")

pool = psycopg2.pool.SimpleConnectionPool(
    2, 
    3, 
    dsn=DATABASE_URL
)

print("Connection pool created successfully using DATABASE_URL")

class Assistant(Agent):
    def __init__(self, db_pool) -> None:
        super().__init__(instructions="You are a helpful medical clinic assistant.")
        self.pool = db_pool

    @function_tool()
    async def list_available_services(
        self,
        context: RunContext,
    ) -> str:
        """
        List all available medical services, including their modality, 
        price, and duration in minutes. Use this when the user asks 
        what services are offered or how much a consultation costs.
        """
        conn = None
        try:
            # 1. Get connection from pool
            conn = self.pool.getconn()
            
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT id, name, price, duration_minutes, modality 
                        FROM services 
                        WHERE is_active = TRUE 
                        ORDER BY name ASC;
                    """)
                    rows = cur.fetchall()

            if not rows:
                return "Sorry, there are no services available at this time."

            # 2. Format for Voice: Using "pesos" and "minutos" makes the TTS sound more natural
            lines = [
                f"{s['id']} {s['name']} en modalidad {s['modality']}, con un costo de {s['price']} pesos y una duración de {s['duration_minutes']} minutos."
                for s in rows
            ]
            
            return "We offer the following services: " + " ".join(lines)

        except Exception as e:
            print(f"❌ Database Error: {e}")
            return "There was a problem accessing the catalog. Please try again in a moment."
            
        finally:
            if conn:
                self.pool.putconn(conn)

    @function_tool()
    async def book_appointment(
        self,
        context: RunContext,
        full_name: str,
        phone: str,
        birth_date: str,
        age: int,
        gender: str,
        service_id: int,
        appointment_date: str,
        appointment_time: str,
        email: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> str:
        """
        Registers a patient and books an appointment. Use this after 
        confirming the date, time, and service with the patient.
        
        Args:
            full_name: Patient's full name.
            phone: Patient's phone number.
            email: Optional email address.
            birth_date: Birth date in YYYY-MM-DD format.
            age: Patient's age.
            gender: Patient's gender (e.g., Male, Female, Other).
            service_id: The ID of the medical service.
            appointment_date: Date of appointment (YYYY-MM-DD).
            appointment_time: Time of appointment (HH:MM).
            reason: Brief reason for the visit.
        """
        conn = None
        try:
            conn = self.pool.getconn()
            conn.autocommit = False # Handle transaction manually
            
            with conn.cursor() as cur:
                # 1. Upsert Patient
                cur.execute("""
                    INSERT INTO patients (full_name, phone, email, birth_date, age, gender) 
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (phone) DO UPDATE SET full_name = EXCLUDED.full_name 
                    RETURNING id;
                """, (full_name, phone, email, birth_date, age, gender))
                patient_id = cur.fetchone()[0]

                # 2. Check availability
                cur.execute("""
                    SELECT id FROM appointments 
                    WHERE appointment_date=%s AND appointment_time=%s AND status!='Cancelled'
                """, (appointment_date, appointment_time))
                
                if cur.fetchone():
                    conn.rollback()
                    return "I'm sorry, that time slot is no longer available. Can we try another time?"

                # 3. Insert Appointment
                cur.execute("""
                    INSERT INTO appointments (patient_id, service_id, appointment_date, appointment_time, status, reason)
                    VALUES (%s, %s, %s, %s, 'Pending', %s) RETURNING id;
                """, (patient_id, service_id, appointment_date, appointment_time, reason))
                appointment_id = cur.fetchone()[0]

                conn.commit()
                
                # 4. Optional: Async Email (Optional: trigger background task instead)
                # send_confirmation_email(...) 
                
                return f"¡Listo! Tu cita ha sido agendada con éxito para el {appointment_date} a las {appointment_time}. Tu número de confirmación es el {appointment_id}."

        except Exception as e:
            if conn:
                conn.rollback()
            print(f"❌ Booking Error: {e}")
            return "Lo siento, tuve un problema técnico al intentar agendar tu cita. Por favor, intenta de nuevo en un momento."
        
        finally:
            if conn:
                self.pool.putconn(conn)

server = AgentServer()

@server.rtc_session(agent_name="telephony-agent")
async def my_agent(ctx: agents.JobContext):
    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            voice="coral"
        )
    )

    await session.start(
        room=ctx.room,
        agent=Assistant(pool),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=lambda params: noise_cancellation.BVCTelephony() if params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP else noise_cancellation.BVC(),
            ),
        ),
    )

    await session.generate_reply(
        instructions="Greet the user and offer your assistance. You should start by speaking in English."
    )


if __name__ == "__main__":
    agents.cli.run_app(server)