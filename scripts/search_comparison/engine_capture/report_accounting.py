"""Final cell-owned report bytes are additional to per-command observations."""
from pathlib import Path

from answer_eval.protocol import EvaluationError
from .protocol import local_path
from .report_streams import LIMITS


def census(root):
    root=local_path(str(root));logical=allocated=files=0
    for path in root.rglob('*'):
        if path.is_symlink():raise EvaluationError('report ownership contains a symlink',status='MISSING')
        if path.is_dir():continue
        if not path.is_file():raise EvaluationError('report ownership contains a special node',status='MISSING')
        stat=path.stat();logical+=stat.st_size;allocated+=stat.st_blocks*512;files+=1
    return {'logical_bytes':logical,'allocated_bytes':allocated,'files':files}


def final(owner,matrix,administration,*,errors,typed_bytes):
    cells=[]
    for view,arm in matrix:
        value=census(Path(owner)/'arms'/view/arm)
        cells.append({'view':view,'arm':arm,**value,
                      'status':'FAIL' if max(value['logical_bytes'],value['allocated_bytes'])>LIMITS['cell_bytes'] else 'PASS'})
    logical=sum(row['bytes'] for row in administration.values())
    allocated=sum((Path(owner)/name).stat().st_blocks*512 for name in administration)
    reserve=LIMITS['header_bytes']
    root={'manifest_limit_bytes':reserve,'total_limit_bytes':LIMITS['root_admin_bytes'],
          'non_manifest_logical_bytes':logical,'non_manifest_allocated_bytes':allocated,
          'manifest_allocation_reserve_bytes':reserve,
          'status':'FAIL' if max(logical,allocated)+reserve>LIMITS['root_admin_bytes'] else 'PASS',
          'method':'complete root administration plus conservative full bounded-manifest logical/allocation reserve; cell raw and derived bytes remain inside each4GiB census'}
    state='FAIL' if root['status']=='FAIL' or any(r['status']=='FAIL' for r in cells) else 'MISSING' if errors else 'VERIFIED'
    return {'status':state,'errors':list(errors),'cells':cells,'root_administration':root,'typed_component_bytes':typed_bytes}
