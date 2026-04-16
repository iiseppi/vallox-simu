import logging
import asyncio
import random
from pymodbus.server import StartAsyncTcpServer
from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusDeviceContext

# --- VALLOX REKISTERIKARTTA (Manual_Modbus_FIN.pdf) ---

# Tilat ja ohjaus
REG_STATE_HOME_AWAY = 4609    # 0=Kotona, 1=Poissa
REG_POWER = 4610              # 0=Päällä, 5=Pois
REG_MODE_BOOST_TIMER = 4612   # Tehostusajastin (min)

# Huolto
REG_FILTER_DAYS = 4620        # Päiviä suodattimen vaihtoon

# Asetusarvot - PUHALLINNOPEUDET (%)
REG_SET_SPEED_AWAY = 20501    # Poissa-tilan asetus %
REG_SET_SPEED_HOME = 20507    # Kotona-tilan asetus %
REG_SET_SPEED_BOOST = 20513   # Tehostus-tilan asetus %

# Asetusarvot - TAVOITELÄMPÖTILAT (cK)
REG_SET_TEMP_AWAY = 20502     # Poissa tavoite
REG_SET_TEMP_HOME = 20508     # Kotona tavoite
REG_SET_TEMP_BOOST = 20514    # Tehostus tavoite

# Mittausarvot (Read-only)
REG_FAN_SPEED = 4353          # Nykyinen puhallinnopeus % (Aktuaalinen)
REG_TEMP_EXTRACT = 4354       # Poistoilma (Sisäilma)
REG_TEMP_EXHAUST = 4355       # Jäteilma (Ulos menevä)
REG_TEMP_OUTDOOR = 4356       # Ulkoilma
REG_TEMP_SUPPLY = 4358        # Tuloilma (Koneelta sisälle)
REG_RH = 4363                 # Kosteus %
REG_CO2 = 4364                # CO2 ppm
REG_FAULT_COUNT = 36865       # Vikojen määrä

def celsius_to_ck(celsius):
    """Muuntaa Celsiuksen senttikelvineiksi: (C * 100) + 27315"""
    return int((celsius * 100) + 27315)

class ValloxSimulator:
    def __init__(self, context):
        self.context = context
        self.slave_id = 1
        
        # --- ALUSTUS (Oletusarvot, jotta HA ei näytä nollia) ---
        
        # Perustilat
        self.set_value(REG_STATE_HOME_AWAY, 0)  # Kotona
        self.set_value(REG_POWER, 0)            # Päällä
        self.set_value(REG_FAULT_COUNT, 0)      # Ei vikoja
        self.set_value(REG_FILTER_DAYS, 180)    # 6kk vaihtoväli
        
        # Nopeusasetukset (Nämä näkyvät HA:n liukusäätimissä)
        self.set_value(REG_SET_SPEED_HOME, 50)
        self.set_value(REG_SET_SPEED_AWAY, 30)
        self.set_value(REG_SET_SPEED_BOOST, 80)
        
        # Lämpötila-asetukset (cK)
        self.set_value(REG_SET_TEMP_HOME, celsius_to_ck(21.0))
        self.set_value(REG_SET_TEMP_AWAY, celsius_to_ck(15.0))
        self.set_value(REG_SET_TEMP_BOOST, celsius_to_ck(20.0))
        
        # Fysiikan muuttujat
        self.temp_outdoor = 2.0
        self.temp_extract = 22.0
        self.co2 = 650
        self.rh = 45
        self.boost_counter = 0
        self.filter_counter = 0

    def set_value(self, address, value):
        self.context[self.slave_id].setValues(3, address, [int(value)])

    def get_value(self, address):
        return self.context[self.slave_id].getValues(3, address, count=1)[0]

    async def run_simulation(self):
        logging.info("Vallox-simulaatio v3.0 käynnissä...")
        print("Data päivittyy 5 sekunnin välein...")
        
        while True:
            # --- 1. LOGIIKKA JA OHJAUS ---
            
            # Onko kone sammutettu pääkytkimestä? (Register 4610: 5=OFF)
            is_power_off = self.get_value(REG_POWER) == 5
            
            if is_power_off:
                mode_text = "SAMMUTETTU"
                target_speed = 0
            else:
                # Luetaan tilat
                is_away = self.get_value(REG_STATE_HOME_AWAY) == 1
                boost_timer_val = self.get_value(REG_MODE_BOOST_TIMER)
                
                # HUOM: Nyt luemme nopeuden ASETUS-rekistereistä, emme kovakoodatuista arvoista!
                # Tämä mahdollistaa säädön Home Assistantista.
                
                if boost_timer_val > 0:
                    target_speed = self.get_value(REG_SET_SPEED_BOOST)
                    mode_text = "TEHOSTUS"
                    # Ajastin logic
                    if boost_timer_val < 65535:
                        self.boost_counter += 1
                        if self.boost_counter >= 12: # n. 1 min
                            self.set_value(REG_MODE_BOOST_TIMER, boost_timer_val - 1)
                            self.boost_counter = 0
                elif is_away:
                    target_speed = self.get_value(REG_SET_SPEED_AWAY)
                    mode_text = "POISSA"
                else:
                    target_speed = self.get_value(REG_SET_SPEED_HOME)
                    mode_text = "KOTONA"

            # Päivitetään aktuaalinen nopeus (Feedback)
            self.set_value(REG_FAN_SPEED, target_speed)

            # Simuloidaan suodattimen kulumista (nopeutettuna demoa varten)
            self.filter_counter += 1
            if self.filter_counter >= 20: # Joka 20. sykli vähentää päivän
                days_left = self.get_value(REG_FILTER_DAYS)
                if days_left > 0:
                    self.set_value(REG_FILTER_DAYS, days_left - 1)
                self.filter_counter = 0

            # --- 2. FYSIIKKAMOOTTORI ---

            if is_power_off:
                # Jos kone kiinni, lämpötilat tasaantuvat hitaasti kohti ympäristöä
                # (Yksinkertaistettu: pysyvät ennallaan)
                pass 
            else:
                # Ulkoilma vaihtelee
                self.temp_outdoor += random.uniform(-0.3, 0.3)
                self.temp_outdoor = max(-5.0, min(5.0, self.temp_outdoor))

                # Sisäilma
                if self.co2 > 800:
                    self.temp_extract += random.uniform(0.0, 0.1)
                else:
                    self.temp_extract += random.uniform(-0.05, 0.05)
                self.temp_extract = max(21.0, min(23.5, self.temp_extract))

                # LTO (Lämmöntalteenotto)
                lto_efficiency = 0.75
                # Jos nopeus on hyvin pieni, hyötysuhde laskee (teoreettinen)
                if target_speed < 20:
                    lto_efficiency = 0.5
                
                delta_t = self.temp_extract - self.temp_outdoor
                self.temp_supply = self.temp_outdoor + (delta_t * lto_efficiency)
                self.temp_exhaust = self.temp_extract - (delta_t * lto_efficiency)

                # CO2 ja Kosteus
                co2_change = random.choice([-15, 25]) 
                self.co2 += co2_change
                
                # Ilmanvaihto poistaa CO2:ta
                ventilation_factor = target_speed / 20.0 
                self.co2 -= ventilation_factor * 5 
                self.co2 = max(410, min(1200, int(self.co2)))
                
                target_rh = 30 + (self.co2 - 400) / 40
                self.rh += (target_rh - self.rh) * 0.1
                self.rh = max(25, min(65, int(self.rh)))

            # --- 3. KIRJOITUS ---
            self.set_value(REG_TEMP_OUTDOOR, celsius_to_ck(self.temp_outdoor))
            self.set_value(REG_TEMP_EXTRACT, celsius_to_ck(self.temp_extract))
            self.set_value(REG_TEMP_SUPPLY, celsius_to_ck(self.temp_supply))
            self.set_value(REG_TEMP_EXHAUST, celsius_to_ck(self.temp_exhaust))
            
            self.set_value(REG_CO2, int(self.co2))
            self.set_value(REG_RH, int(self.rh))

            print(f"[{mode_text}] Nopeus: {target_speed}% | Tavoite Home: {self.get_value(REG_SET_SPEED_HOME)}% | "
                  f"Ulko: {self.temp_outdoor:.1f}C | Tulo: {self.temp_supply:.1f}C")

            await asyncio.sleep(5)

async def main():
    logging.basicConfig(format='%(asctime)s %(message)s', level=logging.INFO)

    store = ModbusDeviceContext(
        hr=ModbusSequentialDataBlock(0, [0]*40000)
    )
    context = ModbusServerContext(store, single=True)

    simulator = ValloxSimulator(context)
    asyncio.create_task(simulator.run_simulation())

    print("--- Vallox Modbus TCP Simulaattori v3.0 (Ultimate) ---")
    print("Palvelin käynnistyy portissa 5020...")
    await StartAsyncTcpServer(context=context, address=("0.0.0.0", 5020))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSimulaattori sammutettu.")
