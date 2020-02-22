#!/usr/bin/env python -u

# Daemon process to manage the production of cutouts from an ASKAP scheduing block

# Author James Dempsey
# Date 27 Jan 2020

import argparse
import datetime

import os
import subprocess
import sys
import time

from astropy.io.votable import parse_single_table


class CommandFailedError(Exception):
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)


def parseargs():
    """
    Parse the command line arguments
    :return: An args map with the parsed arguments
    """
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                     description="Daemon process to manage the production of cutouts for each component from an ASKAP scheduing block")
    parser.add_argument("-d", "--delay", help="Number of seconds to pause between scans for completed jobs",
                        type=int, default=30)
    parser.add_argument("-s", "--sbid", help="The id of the ASKAP scheduling bloc to be processed",
                        type=int, default=8906)
    parser.add_argument("-t", "--status_folder", help="The status folder which will contain the completed files",
                        default='status')
    parser.add_argument("-f", "--filename", help="The name of the votable format file listing the components to be processed.",
                        default='smc_srcs_image_params.vot')
    parser.add_argument("-m", "--max_loops", help="The maximum number of processing loops the daemon will run.",
                        type=int, default=500)
    parser.add_argument("-c", "--concurrency_limit", help="The maximum number of concurrent processes allowed to run.",
                        type=int, default=12)
    parser.add_argument("-d", "--min_concurrency_limit", help="The minumum number of concurrent processes we prefer to run. " +
                        "Duplicate ms usage will be allowed in orer to reach this number of jobs",
                        type=int, default=6)

    parser.add_argument("--pbs", help="Run the jobs via PBS qsub command", default=False,
                        action='store_true')
    parser.add_argument("-l", "--log_folder", help="The folder which will contain the stdout and stderr files from the jobs",
                        default='logs')
    parser.add_argument("-a", "--active", help="The numerical component index of an active cutout job. The job will be monitored as if this daemon started it",
                        type=int, action='append')
    args = parser.parse_args()
    return args


def run_os_cmd(cmd, failOnErr=True):
    """
    Run an operating system command ensuring that it finishes successfully.
    If the comand fails, the program will exit.
    :param cmd: The command to be run
    :return: None
    """
    print(">", cmd)
    sys.stdout.flush()
    try:
        retcode = subprocess.call(cmd, shell=True)
        if retcode != 0:
            message = "Command '"+cmd+"' failed with code " + str(retcode)
            print(message, file=sys.stderr)
            if failOnErr:
                raise CommandFailedError(message)
    except OSError as e:
        message = "Command '" + cmd + "' failed " + e
        print(message, file=sys.stderr)
        if failOnErr:
            raise CommandFailedError(message)
    return None


def read_image_params(filename):
    # targets - array with an entry per source - has a 'component_name' entry per row
    # image_params - array with component_name and beam_ids entries per row - one entry per source/beam combo
    table = parse_single_table(filename, pedantic=False)
    image_params = table.array
    targets = []
    for row in image_params:
        if row['component_name'] not in targets:
            targets.append(row['component_name'])
    return targets, image_params


def build_map(image_params):
    # Build map of sources to beam ids
    src_beam_map = dict()
    for row in image_params:
        comp_name = row['component_name']
        beam_id = row['beam_ids']
        if comp_name not in src_beam_map.keys():
            beams = set()
            src_beam_map[comp_name] = beams
        beams = src_beam_map[comp_name]
        beams.add(beam_id)
    return src_beam_map


def register_active(targets, src_beam_map, active_ids, active_ms, pre_active_jobs):
    if not pre_active_jobs:
        return 0

    for array_id in pre_active_jobs:
        comp_name = targets[array_id-1]
        tgt_ms = src_beam_map[comp_name]

        for ms in tgt_ms:
            active_ms.append(ms)
        active_ids.add(array_id)
        # print ('+++ ' + str(active_ids))
        print('Registered active job {} (#{}) concurrency {} ms: {}'.format(
            comp_name, array_id, len(active_ids), tgt_ms))
    return len(active_ids)


def mark_comp_done(array_id, tgt_ms, active_ids, active_ms):
    active_ids.remove(array_id)
    for ms in tgt_ms:
        active_ms.remove(ms)


def job_loop(targets, sbid, status_folder, src_beam_map, active_ids, active_ms, remaining_array_ids, completed_srcs,
             failed_srcs, concurrency_limit, min_concurrency_limit, use_pbs, log_folder):
    rate_limited = False
    # Take a copy of the list to avoid issues when removing items from it
    ids_to_scan = list(remaining_array_ids)
    # Scan for completed jobs
    for array_id in ids_to_scan:
        comp_name = targets[array_id-1]
        if comp_name in completed_srcs or comp_name in failed_srcs:
            continue

        if os.path.isfile('{}/{:d}.COMPLETED'.format(status_folder, array_id)):
            # print ('--- ' + str(active_ids))
            if array_id in active_ids:
                print('Completed {}  (#{}) concurrency {}'.format(comp_name, array_id, len(active_ids)))
                mark_comp_done(array_id, src_beam_map[comp_name], active_ids, active_ms)
            else:
                print(' Skipping {} (#{}) as it has already completed'.format(comp_name, array_id))
            completed_srcs.add(comp_name)
            remaining_array_ids.remove(array_id)
            continue

        if os.path.isfile('{}/{:d}.FAILED'.format(status_folder, array_id)):
            if array_id in active_ids:
                print('Failed {}  (#{}) concurrency {}'.format(comp_name, array_id, len(active_ids)))
                mark_comp_done(array_id, src_beam_map[comp_name], active_ids, active_ms)
            else:
                print(' Skipping {} (#{}) as it has already failed'.format(comp_name, array_id))
            failed_srcs.add(comp_name)
            remaining_array_ids.remove(array_id)
            continue

    # Scan for jobs to start
    for array_id in remaining_array_ids:
        if array_id in active_ids:
            continue

        comp_name = targets[array_id-1]
        if comp_name in completed_srcs:
            continue


        tgt_ms = src_beam_map[comp_name]
        if len(active_ids) > min_concurrency_limit and tgt_ms & active_ms:
            continue

        if len(active_ids) < concurrency_limit:
            rate_limited = False
            for ms in tgt_ms:
                active_ms.append(ms)
            active_ids.add(array_id)
            # print ('+++ ' + str(active_ids))
            print('Starting {} (#{}) concurrency {} ms: {}'.format(
                comp_name, array_id, len(active_ids), tgt_ms))
            # run_os_cmd('./make_askap_abs_cutout.sh {} {}'.format(array_id, status_folder))
            if use_pbs:
                run_os_cmd(
                    ('qsub -v COMP_INDEX={0} -v SBID={2} -N "ASKAP_abs{0}" -o {1}/askap_abs_{0}_o.log '
                     '-e {1}/askap_abs_{0}_e.log ./start_job.sh').format(array_id, log_folder, sbid))
            else:
                run_os_cmd('./start_job.sh {} {}'.format(array_id, sbid))
        elif not rate_limited:
            rate_limited = True
            print (' rate limit of {} applied'.format(concurrency_limit))
    return len(active_ids)


def produce_all_cutouts(targets, sbid, status_folder, src_beam_map, delay, concurrency_limit, min_concurrency_limit, use_pbs,
                        log_folder, pre_active_jobs, max_loops=500):
    remaining_array_ids = list(range(1, len(targets)+1))
    active_ms = list()
    active_ids = set()
    completed_srcs = set()
    failed_srcs = set()

    total_concurrency = 0
    print('Processing {} targets'.format(len(remaining_array_ids)))

    total_concurrency += register_active(targets, src_beam_map, active_ids, active_ms, pre_active_jobs)
    i = 0
    while len(remaining_array_ids) > 0 and i < max_loops:
        i += 1
        print("\nLoop #{}, completed {} failed {} remaining {} at {}".format(
            i, len(completed_srcs), len(failed_srcs), len(remaining_array_ids), 
            time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))), flush=True)
        total_concurrency += job_loop(targets, sbid, status_folder, src_beam_map, active_ids, active_ms, remaining_array_ids,
                                      completed_srcs, failed_srcs, concurrency_limit, min_concurrency_limit, use_pbs, 
                                      log_folder)
        if len(remaining_array_ids) > 0:
            time.sleep(delay)

    if len(remaining_array_ids) > 0:
        msg = 'ERROR: Failed to complete processing after {} loops. {} cutouts remain'.format(
            i, len(remaining_array_ids))
        print('\n'+msg)
        raise Exception(msg)
    else:
        print('\nCompleted processing in {} loops with average concurrency {:.2f}'.format(
            i, total_concurrency/i))


def main():
    # Parse command line options
    args = parseargs()

    start = time.time()
    print("#### Started ASKAP cutout production at %s ####" %
          time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start)))

    print ('Checking every {} seconds for completed jobs, with a maximum of {} checks.'.format(args.delay, args.max_loops))
    
    # Prepare the run
    targets, image_params = read_image_params(args.filename)
    src_beam_map = build_map(image_params)

    if args.active:
        print ("Already active jobs: {}".format(args.active))
    
    status_folder = '{}/{}'.format(args.status_folder, args.sbid)
    log_folder = '{}/{}'.format(args.log_folder, args.sbid)

    # Run through the processing
    produce_all_cutouts(targets, args.sbid, status_folder, src_beam_map, args.delay, args.concurrency_limit, 
                        args.min_concurrency_limit, args.pbs, 
                        log_folder, args.active, max_loops=args.max_loops)

    # Report
    end = time.time()
    print('#### Processing completed at %s ####' %
          time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end)))
    print('Processed %d components in %.02f s' %
          (len(targets), end - start))
    return 0


if __name__ == '__main__':
    exit(main())
