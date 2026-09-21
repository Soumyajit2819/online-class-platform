import StudentJoinForm from '@/components/StudentJoinForm'

export default function InviteJoinPage({ params }: { params: { inviteCode: string } }) {
  return <StudentJoinForm inviteCode={params.inviteCode} />
}
