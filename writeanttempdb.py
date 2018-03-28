#!/usr/bin/env python
# This script reads the temp from antminers and then writes it to the templog database. Script should be run from cron every X mins for Charting
# like such   */15 * * * * /home/antminer-monitor/writeanttempdb.py

from app.views.antminer_json import (get_summary,
                                     get_pools,
                                     get_stats,
                                     )

from sqlalchemy.exc import IntegrityError
from app.pycgminer import CgminerAPI
from app import app, db, logger, __version__
from app.models import Miner, MinerModel, Settings
import re
from datetime import timedelta
import time

import sqlite3
import Adafruit_DHT

# global variables
dbname='/home/antminer-monitor/app/db/templog.db'

# Update from one unit to the next if the value is greater than 1024.
# e.g. update_unit_and_value(1024, "GH/s") => (1, "TH/s")
def update_unit_and_value(value, unit):
    while value > 1024:
        value = value / 1024.0
        if unit == 'MH/s':
            unit = 'GH/s'
        elif unit == 'GH/s':
            unit = 'TH/s'
        elif unit == 'TH/s':
            unit = 'PH/s'
        elif unit == 'PH/s':
            unit = 'EH/s'
        else:
            assert False, "Unsupported unit: {}".format(unit)
    return (value, unit)



def miners():
    # Init variables
    start = time.clock()
    miners = Miner.query.all()
    models = MinerModel.query.all()
    active_miners = []
    inactive_miners = []
    workers = {}
    miner_chips = {}
    temperatures = {}
    fans = {}
    hash_rates = {}
    hw_error_rates = {}
    uptimes = {}
    total_hash_rate_per_model = {"L3+": {"value": 0, "unit": "MH/s" },
                                "S7": {"value": 0, "unit": "GH/s" },
                                "S9": {"value": 0, "unit": "GH/s" },
                                "D3": {"value": 0, "unit": "MH/s" },
                                "T9": {"value": 0, "unit": "TH/s" },
                                "A3": {"value": 0, "unit": "GH/s" },
                                "L3": {"value": 0, "unit": "MH/s" },}
                                
    errors = False
    miner_errors = {}

    for miner in miners:
        miner_stats = get_stats(miner.ip)
        # if miner not accessible
        if miner_stats['STATUS'][0]['STATUS'] == 'error':
            errors = True
            inactive_miners.append(miner)
        else:
            # Get worker name
            miner_pools = get_pools(miner.ip)
            worker = miner_pools['POOLS'][0]['User']
            # Get miner's ASIC chips
            asic_chains = [miner_stats['STATS'][1][chain] for chain in miner_stats['STATS'][1].keys() if
                           "chain_acs" in chain]
            # count number of working chips
            O = [str(o).count('o') for o in asic_chains]
            Os = sum(O)
            # count number of defective chips
            X = [str(x).count('x') for x in asic_chains]
            Xs = sum(X)
            # get number of in-active chips
            _dash_chips = [str(x).count('-') for x in asic_chains]
            _dash_chips = sum(_dash_chips)
            # Get total number of chips according to miner's model
            # convert miner.model.chips to int list and sum
            chips_list = [int(y) for y in str(miner.model.chips).split(',')]
            total_chips = sum(chips_list)
            # Get the temperatures of the miner according to miner's model
            temps = [int(miner_stats['STATS'][1][temp]) for temp in
                     sorted(miner_stats['STATS'][1].keys(), key=lambda x: str(x)) if
                     re.search(miner.model.temp_keys + '[0-9]', temp) if miner_stats['STATS'][1][temp] != 0]
            # Get fan speeds
            fan_speeds = [miner_stats['STATS'][1][fan] for fan in
                          sorted(miner_stats['STATS'][1].keys(), key=lambda x: str(x)) if
                          re.search("fan" + '[0-9]', fan) if miner_stats['STATS'][1][fan] != 0]
            # Get GH/S 5s
            ghs5s = float(str(miner_stats['STATS'][1]['GHS 5s']))
            # Get HW Errors
            hw_error_rate = miner_stats['STATS'][1]['Device Hardware%']
            # Get uptime
            uptime = timedelta(seconds=miner_stats['STATS'][1]['Elapsed'])
            #
            workers.update({miner.ip: worker})
            miner_chips.update({miner.ip: {'status': {'Os': Os, 'Xs': Xs, '-': _dash_chips},
                                           'total': total_chips,
                                           }
                                })
            temperatures.update({miner.ip: temps})
            fans.update({miner.ip: {"speeds": fan_speeds}})
            value, unit = update_unit_and_value(ghs5s, total_hash_rate_per_model[miner.model.model]['unit'])
            hash_rates.update({miner.ip: "{:3.2f} {}".format(value, unit)})
            hw_error_rates.update({miner.ip: hw_error_rate})
            uptimes.update({miner.ip: uptime})
            total_hash_rate_per_model[miner.model.model]["value"] += ghs5s
            active_miners.append(miner)

            # Flash error messages
            if Xs > 0:
                error_message = "[WARNING] '{}' chips are defective on miner '{}'.".format(Xs, miner.ip)
                logger.warning(error_message)
                flash(error_message, "warning")
                errors = True
                miner_errors.update({miner.ip: error_message})
            if Os + Xs < total_chips:
                error_message = "[ERROR] ASIC chips are missing from miner '{}'. Your Antminer '{}' has '{}/{} chips'." \
                    .format(miner.ip,
                            miner.model.model,
                            Os + Xs,
                            total_chips)
                logger.error(error_message)
                flash(error_message, "error")
                errors = True
                miner_errors.update({miner.ip: error_message})
            if max(temps) >= 80:
                error_message = "[WARNING] High temperatures on miner '{}'.".format(miner.ip)
                logger.warning(error_message)
                flash(error_message, "warning")

    # Flash success/info message
    if not miners:
        error_message = "[INFO] No miners added yet. Please add miners using the above form."
        logger.info(error_message)
        flash(error_message, "info")
    elif not errors:
        error_message = "[INFO] All miners are operating normal. No errors found."
        logger.info(error_message)
    total_hash_rate_per_model_temp = {}
    for key in total_hash_rate_per_model:
        value, unit = update_unit_and_value(total_hash_rate_per_model[key]["value"], total_hash_rate_per_model[key]["unit"])
        if value > 0:
            total_hash_rate_per_model_temp[key] = "{:3.2f} {}".format(value, unit)

# Print out the temperature of the antminers
#       version=__version__,
#       models=models,
#       active_miners=active_miners,
#       inactive_miners=inactive_miners,
#       workers=workers,
#       miner_chips=miner_chips,
#       temperatures=temperatures,
#       fans=fans,
#       hash_rates=hash_rates,
#       hw_error_rates=hw_error_rates,
#       uptimes=uptimes,
#       total_hash_rate_per_model=total_hash_rate_per_model_temp,
#       loading_time=loading_time,
#       miner_errors=miner_errors,

    print "ktest"
    
    antmineravgtemp = []
    antminerip = [] 

    print temperatures
    for k, v in temperatures.items(): 
        tempavg = sum(v)/ float(len(v))
        tempavg = float(tempavg)*9/5+32
        antmineravgtemp.append(round(tempavg,2))
        antminerip.append(k)

    print antminerip[0], antmineravgtemp[0]
    print antminerip[1], antmineravgtemp[1]

#   import pdb
#    pdb.set_trace()
    humm, temp = Adafruit_DHT.read_retry(Adafruit_DHT.AM2302, 4)
    humm = round (humm, 2)
    temp = round (temp, 2)
    tempf =  temp*9/5+32
    antminer1 = antmineravgtemp[0]
    antminer2 = antmineravgtemp[1]

    # store the temperature and hummidity in the database

    conn=sqlite3.connect(dbname)
    curs=conn.cursor()

    curs.execute("INSERT INTO temp_hum values(datetime('now','localtime'), (?), (?),(?),(?))", (tempf,humm,antminer1,antminer2))
    conn.commit()
    conn.close()



# Delete data older than x days
def delete_data():

    conn=sqlite3.connect(dbname)
    curs=conn.cursor()

    curs.execute("DELETE FROM temp_hum WHERE timestamp <=date('now','-7 day')")
    conn.commit()
    conn.close()


# main function
# This is where the program starts
def main():
        kminers = miners()

        # delete data older than X Days
        delete_data()

if __name__=="__main__":
    main()

